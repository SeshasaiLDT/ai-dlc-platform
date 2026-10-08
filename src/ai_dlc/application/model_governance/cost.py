"""Deterministic cost calculation from trusted registry pricing.

Costs are *calculated estimates* from registry price metadata, not authoritative billing; they
must be reconciled against provider billing data where that matters. Rounding is explicit:
results are rounded UP to 6 decimal places (never undercounting). Unknown pricing yields
``None`` (unknown), never zero.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from ai_dlc.application.model_registry import PricingMetadata

from .models import TokenUsage

COST_QUANTUM = Decimal("0.000001")
_MILLION = Decimal(1_000_000)


def _round(value: Decimal) -> Decimal:
    return value.quantize(COST_QUANTUM, rounding=ROUND_CEILING)


def calculate_cost(usage: TokenUsage, pricing: PricingMetadata, currency: str) -> Decimal | None:
    """Cost of reported usage, or None when any used category cannot be priced.

    Cache categories are priced only if the registry carries explicit cache prices; otherwise the
    cost is unknown rather than silently billed at another rate. ``other`` categories have no
    price metadata, so any non-zero amount makes the cost unknown.
    """
    if pricing.currency != currency or not pricing.complete:
        return None
    total = (
        Decimal(usage.input_tokens) * pricing.input_cost_per_million_tokens
        + Decimal(usage.output_tokens) * pricing.output_cost_per_million_tokens
    )
    for count, price in (
        (usage.cache_read_tokens, pricing.cache_read_cost_per_million_tokens),
        (usage.cache_write_tokens, pricing.cache_write_cost_per_million_tokens),
    ):
        if count:
            if price is None:
                return None
            total += Decimal(count) * price
    if any(usage.other.values()):
        return None
    return _round(total / _MILLION)


def estimate_cost(
    input_tokens: int,
    max_output_tokens: int,
    pricing: PricingMetadata,
    currency: str,
    *,
    attempts: int = 1,
) -> Decimal | None:
    """Worst-case pre-call estimate (all output tokens used) times the inner attempts."""
    if pricing.currency != currency or not pricing.complete:
        return None
    base = (
        Decimal(input_tokens) * pricing.input_cost_per_million_tokens
        + Decimal(max_output_tokens) * pricing.output_cost_per_million_tokens
    ) / _MILLION
    return _round(base * attempts)
