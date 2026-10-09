"""Usage, cost, reservation and decision contracts for governed model execution.

Monetary values are Decimal. Missing usage is never zero and unknown cost is never free.
Nothing here carries prompts, credentials or authorization data, and nothing grants permission.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import field_validator, model_validator

from ai_dlc.application.agent_harness import ApprovalIntent, ModelRole, ModelSelectionRequest
from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.application.model_registry import PricingMetadata
from ai_dlc.domain.initiative.models import QuotaLimits

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_TOKENS = 1_000_000_000
MAX_OTHER_CATEGORIES = 8


def plain_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{label} must be a plain identifier")
    return value


def _money(value: Decimal | None, label: str) -> Decimal | None:
    if value is not None and (not value.is_finite() or value < 0):
        raise ValueError(f"{label} must be finite and non-negative")
    return value


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return value


class UsageSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    ESTIMATED = "estimated"
    MISSING = "missing"  # the provider reported nothing; this is NOT zero usage


class AccountingStatus(StrEnum):
    COMMITTED = "committed"  # usage and cost known
    UNKNOWN_COST = "unknown_cost"  # usage known or missing, cost not computable
    UNRECONCILED = "unreconciled"  # outcome uncertain (e.g. timeout); needs reconciliation
    RELEASED = "released"  # known not to have run


class TokenUsage(ContractModel):
    """Provider-reported counts. ``input_tokens`` EXCLUDES cache tokens (adapters normalise)."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    other: dict[str, int] = {}

    @field_validator("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    @classmethod
    def non_negative(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value <= MAX_TOKENS:
            raise ValueError("token counts must be between 0 and 1e9")
        return value

    @field_validator("other")
    @classmethod
    def categories(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > MAX_OTHER_CATEGORIES:
            raise ValueError("too many usage categories")
        for key, count in value.items():
            if not _CATEGORY.match(key) or type(count) is not int or not 0 <= count <= MAX_TOKENS:
                raise ValueError("invalid usage category")
        return value

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + (self.cache_read_tokens or 0)
            + (self.cache_write_tokens or 0)
        )


class UsageRecord(ContractModel):
    usage_id: str
    initiative_id: str
    request_id: str
    correlation_id: str
    task_id: str | None = None
    role: ModelRole
    deployment_id: str
    registry_revision: int
    invocation_ref: str
    usage: TokenUsage | None  # None = missing
    source: UsageSource
    pricing_effective_date: date | None = None
    estimated_cost: Decimal | None = None
    actual_cost: Decimal | None = None  # calculated from registry pricing; not billing-reconciled
    currency: str
    recorded_at: datetime
    status: AccountingStatus

    @field_validator("usage_id", "request_id", "correlation_id", "task_id", "invocation_ref")
    @classmethod
    def ids(cls, value: str | None) -> str | None:
        return None if value is None else plain_id(value, "identifier")

    @field_validator("estimated_cost", "actual_cost")
    @classmethod
    def money(cls, value: Decimal | None) -> Decimal | None:
        return _money(value, "cost")

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value: str) -> str:
        if not _CURRENCY.match(value):
            raise ValueError("currency must be a three-letter uppercase code")
        return value

    @field_validator("recorded_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def consistent(self) -> UsageRecord:
        if (self.usage is None) != (self.source is UsageSource.MISSING):
            raise ValueError("usage is absent exactly when the source is missing")
        if self.actual_cost is not None and self.usage is None:
            raise ValueError("cost cannot be computed without usage")
        if self.status is AccountingStatus.COMMITTED and self.actual_cost is None:
            raise ValueError("a committed record needs a known cost")
        return self


class BudgetState(StrEnum):
    AVAILABLE = "available"
    WARNING = "warning"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


class ReservationState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    UNRECONCILED = "unreconciled"  # still holds its estimate until reconciled


class Reservation(ContractModel):
    reservation_id: str  # stable idempotency identifier
    initiative_id: str
    role: ModelRole
    deployment_id: str
    registry_revision: int
    invocation_ref: str
    estimated_cost: Decimal | None  # None = unknown; holds nothing but is flagged
    estimated_tokens: int
    created_at: datetime
    expires_at: datetime
    budget_period_key: str
    quota_period_key: str
    budget_revision: int
    approval_id: str | None = None
    currency: str = "USD"
    pricing: PricingMetadata | None = None  # snapshot used for this call's cost
    state: ReservationState = ReservationState.RESERVED
    actual_cost: Decimal | None = None
    actual_tokens: int | None = None

    @field_validator("reservation_id", "invocation_ref")
    @classmethod
    def ids(cls, value: str) -> str:
        return plain_id(value, "identifier")

    @field_validator("estimated_cost", "actual_cost")
    @classmethod
    def money(cls, value: Decimal | None) -> Decimal | None:
        return _money(value, "cost")

    @field_validator("created_at", "expires_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _utc(value)


class AdmissionLimits(ContractModel):
    """Effective limits for one reservation, derived by the service from trusted policy."""

    currency: str
    budget_revision: int
    initiative_budget: Decimal | None = None
    role_budget: Decimal | None = None
    enforce_budget: bool = True  # False only for a verified, single-use approved exception
    warning_threshold: float = 0.8
    quota_initiative: QuotaLimits = QuotaLimits()
    quota_role: QuotaLimits = QuotaLimits()
    now: datetime


class LedgerStatus(StrEnum):
    ADMITTED = "admitted"
    DUPLICATE = "duplicate"  # same reservation ID: the existing reservation, nothing reserved twice
    BUDGET_EXHAUSTED = "budget_exhausted"
    QUOTA_EXHAUSTED = "quota_exhausted"
    STALE_BUDGET_REVISION = "stale_budget_revision"
    APPROVAL_REUSED = "approval_reused"


class LedgerAdmission(ContractModel):
    status: LedgerStatus
    budget_state: BudgetState
    reservation: Reservation | None = None
    exhausted_scope: str | None = None  # "initiative" | "role"
    quota_dimension: str | None = None  # invocations | tokens | concurrent | rate
    utilization: float | None = None


class LedgerSnapshot(ContractModel):
    spent: Decimal
    held: Decimal
    invocations: int
    tokens: int
    in_flight: int
    unreconciled: int


class AdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    DUPLICATE = "duplicate"
    BLOCKED_BUDGET = "blocked_budget"
    BLOCKED_QUOTA = "blocked_quota"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_INVALID = "approval_invalid"
    REGISTRY_CHANGED = "registry_changed"
    DEPLOYMENT_UNAVAILABLE = "deployment_unavailable"
    INITIATIVE_MISMATCH = "initiative_mismatch"
    STALE_BUDGET_REVISION = "stale_budget_revision"


class AdmissionRequest(ContractModel):
    """Trusted input from the model-execution layer (never from user text)."""

    selection: ModelSelectionRequest
    deployment_id: str
    evaluated_revision: int  # registry revision the candidate was evaluated at
    estimated_input_tokens: int
    max_output_tokens: int
    invocation_ref: str
    request_cost_limit: Decimal | None = None  # may only lower the organizational limit
    approval_id: str | None = None
    inner_attempts: int = 1

    @field_validator("deployment_id", "invocation_ref")
    @classmethod
    def ids(cls, value: str) -> str:
        return plain_id(value, "identifier")

    @field_validator("estimated_input_tokens", "max_output_tokens")
    @classmethod
    def tokens(cls, value: int) -> int:
        if not 0 <= value <= MAX_TOKENS:
            raise ValueError("token estimates must be between 0 and 1e9")
        return value

    @field_validator("inner_attempts")
    @classmethod
    def attempts(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError("inner_attempts must be between 1 and 10")
        return value

    @field_validator("request_cost_limit")
    @classmethod
    def limit(cls, value: Decimal | None) -> Decimal | None:
        return _money(value, "request_cost_limit")


class AdmissionDecision(ContractModel):
    """Structured operational decision; carries no prompt or authorization data."""

    status: AdmissionStatus
    reason: str
    budget_state: BudgetState = BudgetState.UNKNOWN
    reservation_id: str | None = None
    estimated_cost: Decimal | None = None
    registry_revision: int | None = None
    budget_revision: int | None = None
    utilization: float | None = None
    quota_dimension: str | None = None
    approval_intent: ApprovalIntent | None = None
    initiative_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    task_id: str | None = None

    @property
    def admitted(self) -> bool:
        return self.status in (AdmissionStatus.ADMITTED, AdmissionStatus.DUPLICATE)
