"""Typed, secret-free model deployment records and audit events.

Role and requirement contracts come from the shared harness SDK; nothing is redefined here.
A registry record describes a deployment. It never carries credentials and grants no
permissions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import field_validator, model_validator

from ai_dlc.application.agent_harness import (
    DataClassification,
    DeploymentType,
    InputModality,
    LatencyPreference,
    ModelCapability,
    ModelRole,
    ReasoningLevel,
    ResponseCapability,
)
from ai_dlc.application.agent_harness.models import ContractModel

_DEPLOYMENT_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def validate_correlation_id(value: str) -> str:
    if not isinstance(value, str) or not _CORRELATION_ID.match(value):
        raise ValueError("correlation_id must be a plain identifier")
    return value


def _model_identifier(value: str | None, label: str) -> str | None:
    if value is None:
        return value
    # Configured names only: no ARNs, URLs, or credential-looking strings.
    if not _MODEL_IDENTIFIER.match(value) or value.lower().startswith("arn:") or "//" in value:
        raise ValueError(f"{label} must be a plain configured identifier")
    return value


class OperationalAvailability(StrEnum):
    """Runtime/provider health, deliberately separate from administrative enablement."""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class DeploymentCapabilities(ContractModel):
    structured_output: bool = False
    tool_calling: bool = False
    reasoning: ReasoningLevel = ReasoningLevel.NONE
    input_modalities: frozenset[InputModality] = frozenset({InputModality.TEXT})
    response_capabilities: frozenset[ResponseCapability] = frozenset()

    @field_validator("input_modalities")
    @classmethod
    def has_modality(cls, value: frozenset[InputModality]) -> frozenset[InputModality]:
        if not value:
            raise ValueError("at least one input modality is required")
        return value


class DeploymentContext(ContractModel):
    max_context_tokens: int
    max_output_tokens: int
    reserved_tool_schema_tokens: int = 0
    reserved_protocol_tokens: int = 0

    @model_validator(mode="after")
    def feasible(self) -> DeploymentContext:
        if self.max_context_tokens <= 0 or self.max_output_tokens <= 0:
            raise ValueError("token limits must be positive")
        if self.max_output_tokens >= self.max_context_tokens:
            raise ValueError("max_output_tokens must be smaller than max_context_tokens")
        if self.reserved_tool_schema_tokens < 0 or self.reserved_protocol_tokens < 0:
            raise ValueError("reservations must be non-negative")
        return self


class LatencyMetadata(ContractModel):
    typical_latency_ms: int | None = None
    performance_class: LatencyPreference = LatencyPreference.BALANCED

    @field_validator("typical_latency_ms")
    @classmethod
    def positive(cls, value: int | None) -> int | None:
        if value is not None and not 0 < value <= 3_600_000:
            raise ValueError("typical_latency_ms must be in 1..3600000")
        return value


class PricingMetadata(ContractModel):
    input_cost_per_million_tokens: Decimal | None = None
    output_cost_per_million_tokens: Decimal | None = None
    currency: str = "USD"
    effective_date: date | None = None
    # Only set when the provider prices cache tokens separately; None = not priced.
    cache_read_cost_per_million_tokens: Decimal | None = None
    cache_write_cost_per_million_tokens: Decimal | None = None

    @field_validator(
        "input_cost_per_million_tokens",
        "output_cost_per_million_tokens",
        "cache_read_cost_per_million_tokens",
        "cache_write_cost_per_million_tokens",
    )
    @classmethod
    def non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("prices must be finite and non-negative")
        return value

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be a three-letter uppercase code")
        return value

    @property
    def complete(self) -> bool:
        return (
            self.input_cost_per_million_tokens is not None
            and self.output_cost_per_million_tokens is not None
        )


class DeploymentGovernance(ContractModel):
    supported_classifications: frozenset[DataClassification]
    eligible_roles: frozenset[ModelRole]

    @model_validator(mode="after")
    def nonempty(self) -> DeploymentGovernance:
        if not self.supported_classifications:
            raise ValueError("supported_classifications must not be empty")
        if not self.eligible_roles:
            raise ValueError("eligible_roles must not be empty")
        return self


class ModelDeploymentSpec(ContractModel):
    """Governed metadata describing one deployment (no state, no credentials)."""

    deployment_id: str
    provider_id: str
    model_identifier: str
    inference_profile_id: str | None = None
    # Explicit trusted family metadata (never parsed from names); None means unknown.
    model_family: str | None = None
    region: str
    deployment_type: DeploymentType
    capabilities: DeploymentCapabilities
    context: DeploymentContext
    latency: LatencyMetadata = LatencyMetadata()
    pricing: PricingMetadata = PricingMetadata()
    governance: DeploymentGovernance

    @field_validator("deployment_id")
    @classmethod
    def deployment_id_format(cls, value: str) -> str:
        if not _DEPLOYMENT_ID.match(value) or len(value) > 80:
            raise ValueError("deployment_id must be a lowercase kebab-case identifier")
        return value

    @field_validator("provider_id")
    @classmethod
    def provider_format(cls, value: str) -> str:
        if not _PROVIDER.match(value) or len(value) > 80:
            raise ValueError("provider_id must be a lowercase kebab-case identifier")
        return value

    @field_validator("model_identifier")
    @classmethod
    def model_format(cls, value: str) -> str:
        return _model_identifier(value, "model_identifier") or value

    @field_validator("model_family")
    @classmethod
    def family_format(cls, value: str | None) -> str | None:
        if value is not None and not _PROVIDER.match(value):
            raise ValueError("model_family must be a lowercase kebab-case identifier")
        return value

    @field_validator("inference_profile_id")
    @classmethod
    def profile_format(cls, value: str | None) -> str | None:
        return _model_identifier(value, "inference_profile_id")

    @field_validator("region")
    @classmethod
    def region_format(cls, value: str) -> str:
        if not _REGION.match(value):
            raise ValueError("region must be a plain identifier")
        return value

    def to_model_capability(self, *, reserved_output_tokens: int) -> ModelCapability:
        """Adapt to the harness budgeting contract; the caller picks the output reservation."""
        if reserved_output_tokens > self.context.max_output_tokens:
            raise ValueError("reserved_output_tokens exceeds the deployment's output capacity")
        return ModelCapability(
            model_id=self.model_identifier,
            max_context_tokens=self.context.max_context_tokens,
            reserved_output_tokens=reserved_output_tokens,
            reserved_tool_schema_tokens=self.context.reserved_tool_schema_tokens,
            reserved_protocol_tokens=self.context.reserved_protocol_tokens,
        )


class RegisteredModel(ContractModel):
    """A spec plus administrative and operational state, versioned by ``revision``."""

    spec: ModelDeploymentSpec
    enabled: bool = False
    availability: OperationalAvailability = OperationalAvailability.UNKNOWN
    revision: int
    updated_at: datetime

    @field_validator("revision")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("revision must be positive")
        return value

    @field_validator("updated_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("updated_at must be timezone-aware UTC")
        return value

    @property
    def deployment_id(self) -> str:
        return self.spec.deployment_id


class RegistryOperation(StrEnum):
    REGISTER = "register"
    UPDATE_METADATA = "update_metadata"
    ENABLE = "enable"
    DISABLE = "disable"
    UPDATE_AVAILABILITY = "update_availability"


@dataclass(frozen=True, slots=True)
class ModelRegistryAuditEvent:
    """Audit-safe mutation record: field *names* only, never values."""

    deployment_id: str
    operation: RegistryOperation
    actor_id: str
    occurred_at: datetime
    previous_revision: int | None
    new_revision: int
    changed_fields: tuple[str, ...]
    correlation_id: str
    authorization_decision_id: str

    def __post_init__(self) -> None:
        if self.occurred_at.utcoffset() != timedelta(0):
            raise ValueError("audit timestamp must be timezone-aware UTC")
        validate_correlation_id(self.correlation_id)
        if self.new_revision < 1 or (
            self.previous_revision is not None and self.new_revision <= self.previous_revision
        ):
            raise ValueError("audit revisions must increase")


def utc_now() -> datetime:
    return datetime.now(UTC)
