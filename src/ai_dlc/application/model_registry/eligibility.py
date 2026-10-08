"""Reusable, side-effect-free eligibility evaluation. It filters; it does not rank or route."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ai_dlc.application.agent_harness import DataClassification, ModelRequirements

from .models import OperationalAvailability, RegisteredModel


class IneligibleReason(StrEnum):
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    ROLE_NOT_DECLARED = "role_not_declared"
    STRUCTURED_OUTPUT = "structured_output_unsupported"
    TOOL_CALLING = "tool_calling_unsupported"
    REASONING_LEVEL = "reasoning_level_insufficient"
    INPUT_MODALITY = "input_modality_unsupported"
    RESPONSE_CAPABILITY = "response_capability_unsupported"
    CONTEXT_WINDOW = "context_window_insufficient"
    OUTPUT_CAPACITY = "output_capacity_insufficient"
    CLASSIFICATION = "data_classification_not_permitted"
    DEPLOYMENT_TYPE = "deployment_type_not_permitted"
    PROVIDER = "provider_not_permitted"
    REGION = "region_not_permitted"
    PRICING_MISSING = "pricing_metadata_missing"
    CURRENCY_MISMATCH = "pricing_currency_mismatch"
    COST_CEILING = "cost_ceiling_exceeded"
    LATENCY_UNKNOWN = "latency_unknown"
    LATENCY_CEILING = "latency_ceiling_exceeded"
    NOT_SEPARATE = "not_separate_from_generator"


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    deployment_id: str
    eligible: bool
    reasons: tuple[IneligibleReason, ...]


def evaluate_eligibility(
    model: RegisteredModel,
    requirements: ModelRequirements,
    *,
    data_classification: DataClassification,
    separate_from: Iterable[str] = (),
) -> EligibilityResult:
    """Check every effective requirement; unknown or missing metadata fails closed."""
    spec = model.spec
    caps, need = spec.capabilities, requirements.capabilities
    gov, rules = spec.governance, requirements.governance
    reasons: list[IneligibleReason] = []

    def fail(condition: bool, reason: IneligibleReason) -> None:
        if condition:
            reasons.append(reason)

    fail(not model.enabled, IneligibleReason.DISABLED)
    fail(model.availability is OperationalAvailability.UNAVAILABLE, IneligibleReason.UNAVAILABLE)
    fail(requirements.role not in gov.eligible_roles, IneligibleReason.ROLE_NOT_DECLARED)
    fail(need.structured_output and not caps.structured_output, IneligibleReason.STRUCTURED_OUTPUT)
    fail(need.tool_calling and not caps.tool_calling, IneligibleReason.TOOL_CALLING)
    fail(caps.reasoning < need.reasoning, IneligibleReason.REASONING_LEVEL)
    fail(not need.input_modalities <= caps.input_modalities, IneligibleReason.INPUT_MODALITY)
    fail(
        not need.response_capabilities <= caps.response_capabilities,
        IneligibleReason.RESPONSE_CAPABILITY,
    )
    fail(
        spec.context.max_context_tokens < requirements.context.min_context_tokens,
        IneligibleReason.CONTEXT_WINDOW,
    )
    fail(
        spec.context.max_output_tokens < requirements.context.min_output_tokens,
        IneligibleReason.OUTPUT_CAPACITY,
    )
    fail(
        data_classification not in gov.supported_classifications
        or data_classification not in rules.allowed_classifications,
        IneligibleReason.CLASSIFICATION,
    )
    fail(
        spec.deployment_type not in rules.allowed_deployment_types, IneligibleReason.DEPLOYMENT_TYPE
    )
    fail(
        spec.provider_id in rules.denied_providers
        or (bool(rules.allowed_providers) and spec.provider_id not in rules.allowed_providers),
        IneligibleReason.PROVIDER,
    )
    fail(
        spec.region in rules.denied_regions
        or (bool(rules.allowed_regions) and spec.region not in rules.allowed_regions),
        IneligibleReason.REGION,
    )

    cost, pricing = requirements.cost, spec.pricing
    has_ceiling = (
        cost.max_input_cost_per_million_tokens is not None
        or cost.max_output_cost_per_million_tokens is not None
    )
    if cost.require_pricing_metadata or has_ceiling:
        fail(not pricing.complete, IneligibleReason.PRICING_MISSING)
    if has_ceiling:
        if pricing.currency != cost.currency:
            reasons.append(IneligibleReason.CURRENCY_MISMATCH)
        else:
            for price, ceiling in (
                (pricing.input_cost_per_million_tokens, cost.max_input_cost_per_million_tokens),
                (pricing.output_cost_per_million_tokens, cost.max_output_cost_per_million_tokens),
            ):
                if ceiling is not None and (price is None or price > ceiling):
                    reasons.append(IneligibleReason.COST_CEILING)
                    break

    max_latency = requirements.latency.max_latency_ms
    if max_latency is not None:
        typical = spec.latency.typical_latency_ms
        if typical is None:
            reasons.append(IneligibleReason.LATENCY_UNKNOWN)
        elif typical > max_latency:
            reasons.append(IneligibleReason.LATENCY_CEILING)

    fail(spec.deployment_id in frozenset(separate_from), IneligibleReason.NOT_SEPARATE)

    unique = tuple(dict.fromkeys(reasons))
    return EligibilityResult(spec.deployment_id, not unique, unique)
