"""Governed model execution: budgets, quotas, usage/cost accounting and safe fallback."""

from .client import AttemptRecord, GovernedModelClient, GovernedOutcome, GovernedResult
from .cost import COST_QUANTUM, calculate_cost, estimate_cost
from .fallback import (
    NEVER_FALLBACK,
    CandidatePlan,
    FallbackPlanner,
    ModelFailure,
    ReviewerConstraints,
    classify_model_failure,
    fallback_permitted,
)
from .models import (
    AccountingStatus,
    AdmissionDecision,
    AdmissionLimits,
    AdmissionRequest,
    AdmissionStatus,
    BudgetState,
    LedgerAdmission,
    LedgerSnapshot,
    LedgerStatus,
    Reservation,
    ReservationState,
    TokenUsage,
    UsageRecord,
    UsageSource,
)
from .ports import GovernanceLedger
from .service import ModelGovernanceService, period_key

__all__ = [
    "COST_QUANTUM",
    "NEVER_FALLBACK",
    "AccountingStatus",
    "AdmissionDecision",
    "AdmissionLimits",
    "AdmissionRequest",
    "AdmissionStatus",
    "AttemptRecord",
    "BudgetState",
    "CandidatePlan",
    "FallbackPlanner",
    "GovernanceLedger",
    "GovernedModelClient",
    "GovernedOutcome",
    "GovernedResult",
    "LedgerAdmission",
    "LedgerSnapshot",
    "LedgerStatus",
    "ModelFailure",
    "ModelGovernanceService",
    "Reservation",
    "ReservationState",
    "ReviewerConstraints",
    "TokenUsage",
    "UsageRecord",
    "UsageSource",
    "calculate_cost",
    "classify_model_failure",
    "estimate_cost",
    "fallback_permitted",
    "period_key",
]
