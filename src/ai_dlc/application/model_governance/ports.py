"""Narrow ledger port for budget reservations, usage commits and quota counters."""

from typing import Protocol

from .models import (
    AdmissionLimits,
    LedgerAdmission,
    LedgerSnapshot,
    Reservation,
    ReservationState,
    UsageRecord,
)


class GovernanceLedger(Protocol):
    """Shared accounting store. Every method is one atomic, conditional operation.

    A durable adapter MUST: make ``reserve`` check-and-hold across ALL scopes in one transaction
    (otherwise concurrent requests spend the same remaining budget), key reservations and usage
    records by their stable IDs so replays never double-count, enforce single-use approval IDs and
    monotonic budget revisions inside that transaction, and be shared across runtimes. Process-local
    locks (the in-memory adapter) prove none of that.
    """

    def reserve(self, reservation: Reservation, limits: AdmissionLimits) -> LedgerAdmission: ...

    def commit(
        self,
        reservation_id: str,
        record: UsageRecord,
        *,
        actual_cost,
        actual_tokens: int | None,
    ) -> UsageRecord:
        """RESERVED -> COMMITTED (cost known) or UNRECONCILED (cost or outcome unknown).

        Idempotent: a replay returns the stored record and never counts twice.
        """

    def release(self, reservation_id: str) -> ReservationState:
        """RESERVED -> RELEASED. Committed or unreconciled reservations are left unchanged."""

    def reconcile(
        self,
        reservation_id: str,
        record: UsageRecord,
        *,
        actual_cost,
        actual_tokens: int | None,
    ) -> UsageRecord:
        """UNRECONCILED -> COMMITTED/RELEASED using authoritative information. Idempotent."""

    def get(self, reservation_id: str) -> Reservation | None: ...

    def snapshot(
        self, initiative_id: str, role: str | None, budget_period_key: str, quota_period_key: str
    ) -> LedgerSnapshot: ...

    def usage_records(self, initiative_id: str) -> tuple[UsageRecord, ...]: ...
