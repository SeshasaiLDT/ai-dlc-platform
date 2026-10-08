"""Vendor-neutral model roles and the requirements each role places on a model.

A *role* says what an agent needs from a model; it never names a model, vendor, deployment or
credential. Concrete model deployments are configuration resolved by later routing/registry
components. Roles confer no authorization and never alter ``AgentContext``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from enum import IntEnum, StrEnum

from pydantic import field_validator, model_validator

from .context_budget import ModelCapability
from .models import ContractModel

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_LATENCY_MS = 3_600_000


class ModelRole(StrEnum):
    """Stable, agent-agnostic roles. The value is what ``ModelProvider.invoke_model`` receives."""

    ROUTING = "routing"
    STANDARD_REASONING = "standard_reasoning"
    DEEP_REASONING = "deep_reasoning"
    INDEPENDENT_REVIEWER = "independent_reviewer"


class ReasoningLevel(IntEnum):
    NONE = 0
    BASIC = 1
    MODERATE = 2
    EXTENDED = 3


class InputModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"


class ResponseCapability(StrEnum):
    STREAMING = "streaming"
    CITATIONS = "citations"


class LatencyPreference(StrEnum):
    LOWEST = "lowest"
    BALANCED = "balanced"
    RELAXED = "relaxed"


class CostSensitivity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class DeploymentType(StrEnum):
    """How a model is hosted; the platform's own taxonomy, not a vendor's."""

    MANAGED_SERVICE = "managed_service"
    PRIVATE_ENDPOINT = "private_endpoint"
    SELF_HOSTED = "self_hosted"


def _identifiers(values: frozenset[str], label: str) -> frozenset[str]:
    for value in values:
        if not _IDENTIFIER.match(value):
            raise ValueError(f"{label} entries must be plain identifiers: {value!r}")
    return values


class CapabilityRequirements(ContractModel):
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


class ContextRequirements(ContractModel):
    min_context_tokens: int
    min_output_tokens: int

    @model_validator(mode="after")
    def feasible(self) -> ContextRequirements:
        if self.min_context_tokens <= 0 or self.min_output_tokens <= 0:
            raise ValueError("token requirements must be positive")
        if self.min_output_tokens >= self.min_context_tokens:
            raise ValueError("min_output_tokens must be smaller than min_context_tokens")
        return self

    def is_satisfied_by(self, capability: ModelCapability) -> bool:
        """Compare against a deployment-supplied ``ModelCapability`` (the budgeting contract)."""
        return (
            capability.max_context_tokens >= self.min_context_tokens
            and capability.reserved_output_tokens >= self.min_output_tokens
        )


class LatencyRequirements(ContractModel):
    preference: LatencyPreference = LatencyPreference.BALANCED
    max_latency_ms: int | None = None
    deadline_required: bool = False

    @field_validator("max_latency_ms")
    @classmethod
    def sane(cls, value: int | None) -> int | None:
        if value is not None and not 0 < value <= _MAX_LATENCY_MS:
            raise ValueError(f"max_latency_ms must be in 1..{_MAX_LATENCY_MS}")
        return value


class CostRequirements(ContractModel):
    sensitivity: CostSensitivity = CostSensitivity.MEDIUM
    max_input_cost_per_million_tokens: Decimal | None = None
    max_output_cost_per_million_tokens: Decimal | None = None
    currency: str = "USD"
    require_pricing_metadata: bool = True

    @field_validator("max_input_cost_per_million_tokens", "max_output_cost_per_million_tokens")
    @classmethod
    def non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("cost ceilings must be finite and non-negative")
        return value

    @field_validator("currency")
    @classmethod
    def iso_like(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be a three-letter uppercase code")
        return value

    @model_validator(mode="after")
    def ceilings_need_pricing(self) -> CostRequirements:
        has_ceiling = (
            self.max_input_cost_per_million_tokens is not None
            or self.max_output_cost_per_million_tokens is not None
        )
        if has_ceiling and not self.require_pricing_metadata:
            raise ValueError("cost ceilings require pricing metadata")
        return self


class DataGovernance(ContractModel):
    """Restrictions are configuration; empty allow-lists mean 'no restriction on that axis'."""

    allowed_classifications: frozenset[DataClassification]
    allowed_deployment_types: frozenset[DeploymentType] = frozenset(DeploymentType)
    allowed_providers: frozenset[str] = frozenset()
    denied_providers: frozenset[str] = frozenset()
    allowed_regions: frozenset[str] = frozenset()
    denied_regions: frozenset[str] = frozenset()

    @field_validator("allowed_providers", "denied_providers")
    @classmethod
    def provider_ids(cls, value: frozenset[str]) -> frozenset[str]:
        return _identifiers(value, "provider")

    @field_validator("allowed_regions", "denied_regions")
    @classmethod
    def region_ids(cls, value: frozenset[str]) -> frozenset[str]:
        return _identifiers(value, "region")

    @model_validator(mode="after")
    def consistent(self) -> DataGovernance:
        if not self.allowed_classifications:
            raise ValueError("allowed_classifications must not be empty")
        if not self.allowed_deployment_types:
            raise ValueError("allowed_deployment_types must not be empty")
        if self.allowed_providers & self.denied_providers:
            raise ValueError("a provider cannot be both allowed and denied")
        if self.allowed_regions & self.denied_regions:
            raise ValueError("a region cannot be both allowed and denied")
        return self


class ModelRequirements(ContractModel):
    """What one role needs. Contains no model names, deployments, ARNs or secrets."""

    role: ModelRole
    capabilities: CapabilityRequirements
    context: ContextRequirements
    latency: LatencyRequirements
    cost: CostRequirements
    governance: DataGovernance
    require_separate_from_generator: bool = False


_ALL_CLASSES = frozenset(DataClassification)


def default_role_requirements() -> dict[ModelRole, ModelRequirements]:
    """Configurable defaults; deployments override them and nothing assumes a vendor."""
    governance = DataGovernance(allowed_classifications=_ALL_CLASSES)
    return {
        ModelRole.ROUTING: ModelRequirements(
            role=ModelRole.ROUTING,
            capabilities=CapabilityRequirements(
                structured_output=True, reasoning=ReasoningLevel.BASIC
            ),
            context=ContextRequirements(min_context_tokens=8_000, min_output_tokens=512),
            latency=LatencyRequirements(preference=LatencyPreference.LOWEST, max_latency_ms=5_000),
            cost=CostRequirements(sensitivity=CostSensitivity.HIGH),
            governance=governance,
        ),
        ModelRole.STANDARD_REASONING: ModelRequirements(
            role=ModelRole.STANDARD_REASONING,
            capabilities=CapabilityRequirements(
                structured_output=True, reasoning=ReasoningLevel.MODERATE
            ),
            context=ContextRequirements(min_context_tokens=32_000, min_output_tokens=4_096),
            latency=LatencyRequirements(preference=LatencyPreference.BALANCED),
            cost=CostRequirements(sensitivity=CostSensitivity.MEDIUM),
            governance=governance,
        ),
        ModelRole.DEEP_REASONING: ModelRequirements(
            role=ModelRole.DEEP_REASONING,
            capabilities=CapabilityRequirements(
                structured_output=True,
                tool_calling=True,
                reasoning=ReasoningLevel.EXTENDED,
            ),
            context=ContextRequirements(min_context_tokens=128_000, min_output_tokens=16_384),
            latency=LatencyRequirements(preference=LatencyPreference.RELAXED),
            cost=CostRequirements(sensitivity=CostSensitivity.LOW),
            governance=governance,
        ),
        ModelRole.INDEPENDENT_REVIEWER: ModelRequirements(
            role=ModelRole.INDEPENDENT_REVIEWER,
            capabilities=CapabilityRequirements(
                structured_output=True, reasoning=ReasoningLevel.EXTENDED
            ),
            context=ContextRequirements(min_context_tokens=64_000, min_output_tokens=8_192),
            latency=LatencyRequirements(preference=LatencyPreference.RELAXED),
            cost=CostRequirements(sensitivity=CostSensitivity.MEDIUM),
            governance=governance,
            require_separate_from_generator=True,
        ),
    }


class RoleProfiles(ContractModel):
    """A complete, validated set of per-role requirements (one entry per ``ModelRole``)."""

    profiles: Mapping[ModelRole, ModelRequirements]

    @model_validator(mode="after")
    def complete_and_keyed(self) -> RoleProfiles:
        if set(self.profiles) != set(ModelRole):
            raise ValueError("profiles must define exactly the four model roles")
        for role, requirements in self.profiles.items():
            if requirements.role is not role:
                raise ValueError(f"profile for {role.value} declares role {requirements.role}")
        return self

    @classmethod
    def defaults(cls) -> RoleProfiles:
        return cls(profiles=default_role_requirements())

    def for_role(self, role: ModelRole) -> ModelRequirements:
        return self.profiles[role]


class ModelSelectionRequest(ContractModel):
    """Trusted input for future routing/registry components; carries no selection logic.

    A request may only tighten its role profile. It never carries identity, initiative or
    permissions, and ``AgentContext`` is not read or changed.
    """

    role: ModelRole
    required_capabilities: CapabilityRequirements | None = None
    requested_context_tokens: int | None = None
    requested_output_tokens: int | None = None
    data_classification: DataClassification
    max_latency_ms: int | None = None
    max_input_cost_per_million_tokens: Decimal | None = None
    max_output_cost_per_million_tokens: Decimal | None = None
    allowed_providers: frozenset[str] = frozenset()
    denied_providers: frozenset[str] = frozenset()
    allowed_regions: frozenset[str] = frozenset()
    separate_from_deployments: frozenset[str] = frozenset()

    @field_validator("allowed_providers", "denied_providers")
    @classmethod
    def provider_ids(cls, value: frozenset[str]) -> frozenset[str]:
        return _identifiers(value, "provider")

    @field_validator("allowed_regions")
    @classmethod
    def region_ids(cls, value: frozenset[str]) -> frozenset[str]:
        return _identifiers(value, "region")

    @field_validator("separate_from_deployments")
    @classmethod
    def deployment_ids(cls, value: frozenset[str]) -> frozenset[str]:
        return _identifiers(value, "deployment")

    @field_validator("requested_context_tokens", "requested_output_tokens")
    @classmethod
    def positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("requested token counts must be positive")
        return value

    def effective_requirements(self, profile: ModelRequirements) -> ModelRequirements:
        """Merge this request into ``profile``, tightening only; raise if it cannot be met."""
        if profile.role is not self.role:
            raise ValueError("profile role does not match the requested role")
        gov = profile.governance
        if self.data_classification not in gov.allowed_classifications:
            raise ValueError(f"role {self.role.value} does not permit this data classification")
        allowed_providers = gov.allowed_providers
        if self.allowed_providers:
            allowed_providers = (
                self.allowed_providers & allowed_providers
                if allowed_providers
                else self.allowed_providers
            )
            if not allowed_providers:
                raise ValueError("requested providers are outside the role's allowed providers")
        allowed_regions = gov.allowed_regions
        if self.allowed_regions:
            allowed_regions = (
                self.allowed_regions & allowed_regions if allowed_regions else self.allowed_regions
            )
            if not allowed_regions:
                raise ValueError("requested regions are outside the role's allowed regions")
        denied_providers = gov.denied_providers | self.denied_providers
        eligible_providers = allowed_providers - denied_providers
        if allowed_providers and not eligible_providers:
            raise ValueError("no provider remains eligible after restrictions")
        eligible_regions = allowed_regions - gov.denied_regions
        if allowed_regions and not eligible_regions:
            raise ValueError("no region remains eligible after restrictions")
        # Rebuilt through the validated constructor so every invariant is re-checked.
        governance = DataGovernance(
            allowed_classifications=gov.allowed_classifications,
            allowed_deployment_types=gov.allowed_deployment_types,
            allowed_providers=eligible_providers,
            denied_providers=denied_providers,
            allowed_regions=eligible_regions,
            denied_regions=gov.denied_regions,
        )

        context = profile.context
        context = ContextRequirements(
            min_context_tokens=max(context.min_context_tokens, self.requested_context_tokens or 0),
            min_output_tokens=max(context.min_output_tokens, self.requested_output_tokens or 0),
        )

        latency = profile.latency
        if self.max_latency_ms is not None:
            ceiling = (
                self.max_latency_ms
                if latency.max_latency_ms is None
                else min(latency.max_latency_ms, self.max_latency_ms)
            )
            latency = LatencyRequirements(
                preference=latency.preference,
                max_latency_ms=ceiling,
                deadline_required=latency.deadline_required,
            )

        cost = profile.cost
        cost = CostRequirements(
            sensitivity=cost.sensitivity,
            currency=cost.currency,
            require_pricing_metadata=cost.require_pricing_metadata
            or self.max_input_cost_per_million_tokens is not None
            or self.max_output_cost_per_million_tokens is not None,
            max_input_cost_per_million_tokens=_tighter(
                cost.max_input_cost_per_million_tokens, self.max_input_cost_per_million_tokens
            ),
            max_output_cost_per_million_tokens=_tighter(
                cost.max_output_cost_per_million_tokens, self.max_output_cost_per_million_tokens
            ),
        )

        capabilities = profile.capabilities
        if self.required_capabilities is not None:
            wanted = self.required_capabilities
            capabilities = CapabilityRequirements(
                structured_output=capabilities.structured_output or wanted.structured_output,
                tool_calling=capabilities.tool_calling or wanted.tool_calling,
                reasoning=max(capabilities.reasoning, wanted.reasoning),
                input_modalities=capabilities.input_modalities | wanted.input_modalities,
                response_capabilities=capabilities.response_capabilities
                | wanted.response_capabilities,
            )

        return ModelRequirements(
            role=profile.role,
            capabilities=capabilities,
            context=context,
            latency=latency,
            cost=cost,
            governance=governance,
            require_separate_from_generator=profile.require_separate_from_generator,
        )


def _tighter(current: Decimal | None, requested: Decimal | None) -> Decimal | None:
    if requested is None:
        return current
    if requested < 0 or not requested.is_finite():
        raise ValueError("cost ceilings must be finite and non-negative")
    return requested if current is None else min(current, requested)
