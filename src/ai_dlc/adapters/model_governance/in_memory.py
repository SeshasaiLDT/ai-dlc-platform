"""Deterministic in-memory governance ledger for tests and local use.

Atomic only within one process (a single lock). It does NOT provide distributed budget or quota
enforcement: concurrent runtimes would each have their own ledger.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from threading import Lock

from ai_dlc.application.model_governance.models import (
    AdmissionLimits,
    BudgetState,
    LedgerAdmission,
    LedgerSnapshot,
    LedgerStatus,
    Reservation,
    ReservationState,
    UsageRecord,
)
from ai_dlc.domain.initiative.models import QuotaLimits

_HOLDING = (ReservationState.RESERVED, ReservationState.UNRECONCILED)


class InMemoryGovernanceLedger:
    def __init__(self) -> None:
        self._reservations: dict[str, Reservation] = {}
        self._records: dict[str, UsageRecord] = {}  # latest record per reservation
        self._log: list[UsageRecord] = []
        self._approvals: dict[str, str] = {}
        self._revisions: dict[str, int] = {}
        self._lock = Lock()

    # -- scope arithmetic (callers hold the lock) ------------------------------------------

    def _scope(self, initiative_id: str, role: str | None, budget_key: str, quota_key: str):
        return (
            [
                r
                for r in self._reservations.values()
                if r.initiative_id == initiative_id and (role is None or r.role.value == role)
            ],
            budget_key,
            quota_key,
        )

    def _numbers(self, initiative_id, role, budget_key, quota_key, now) -> LedgerSnapshot:
        items, _, _ = self._scope(initiative_id, role, budget_key, quota_key)
        spent = held = Decimal(0)
        invocations = tokens = in_flight = unreconciled = 0
        for r in items:
            if r.budget_period_key == budget_key:
                if r.state is ReservationState.COMMITTED:
                    spent += r.actual_cost or Decimal(0)
                elif r.state in _HOLDING:
                    held += r.estimated_cost or Decimal(0)
            if r.quota_period_key == quota_key and r.state is not ReservationState.RELEASED:
                invocations += 1
                tokens += (
                    r.actual_tokens or 0
                    if r.state is ReservationState.COMMITTED
                    else r.estimated_tokens
                )
            if r.state is ReservationState.RESERVED and r.expires_at > now:
                in_flight += 1
            if r.state is ReservationState.UNRECONCILED:
                unreconciled += 1
        return LedgerSnapshot(
            spent=spent,
            held=held,
            invocations=invocations,
            tokens=tokens,
            in_flight=in_flight,
            unreconciled=unreconciled,
        )

    def _rate(self, initiative_id: str, role: str | None, now) -> int:
        horizon = now - timedelta(seconds=60)
        return sum(
            1
            for r in self._reservations.values()
            if r.initiative_id == initiative_id
            and (role is None or r.role.value == role)
            and r.state is not ReservationState.RELEASED
            and r.created_at > horizon
        )

    def _sweep(self, now) -> None:
        """Abandoned reservations keep their money held (outcome unknown) but free their slot."""
        for key, r in list(self._reservations.items()):
            if r.state is ReservationState.RESERVED and r.expires_at <= now:
                self._reservations[key] = r.model_copy(
                    update={"state": ReservationState.UNRECONCILED}
                )

    @staticmethod
    def _quota_violation(limits: QuotaLimits, snap: LedgerSnapshot, rate: int, est_tokens: int):
        if limits.max_invocations is not None and snap.invocations + 1 > limits.max_invocations:
            return "invocations"
        if limits.max_tokens is not None and snap.tokens + est_tokens > limits.max_tokens:
            return "tokens"
        if limits.max_concurrent is not None and snap.in_flight + 1 > limits.max_concurrent:
            return "concurrent"
        if limits.max_requests_per_minute is not None and rate + 1 > limits.max_requests_per_minute:
            return "rate"
        return None

    # -- port ------------------------------------------------------------------------------

    def reserve(self, reservation: Reservation, limits: AdmissionLimits) -> LedgerAdmission:
        with self._lock:
            self._sweep(limits.now)
            existing = self._reservations.get(reservation.reservation_id)
            if existing is not None:
                return LedgerAdmission(
                    status=LedgerStatus.DUPLICATE,
                    budget_state=BudgetState.AVAILABLE,
                    reservation=existing,
                )
            if limits.budget_revision < self._revisions.get(reservation.initiative_id, 0):
                return LedgerAdmission(
                    status=LedgerStatus.STALE_BUDGET_REVISION, budget_state=BudgetState.UNKNOWN
                )
            if (
                reservation.approval_id is not None
                and self._approvals.get(reservation.approval_id, reservation.invocation_ref)
                != reservation.invocation_ref
            ):
                return LedgerAdmission(
                    status=LedgerStatus.APPROVAL_REUSED, budget_state=BudgetState.UNKNOWN
                )
            init, role = reservation.initiative_id, reservation.role.value
            bkey, qkey = reservation.budget_period_key, reservation.quota_period_key
            estimate = reservation.estimated_cost or Decimal(0)
            state = BudgetState.AVAILABLE
            top_utilization: float | None = None
            if limits.enforce_budget:
                for scope, scope_role, cap in (
                    ("initiative", None, limits.initiative_budget),
                    ("role", role, limits.role_budget),
                ):
                    if cap is None:
                        continue
                    snap = self._numbers(init, scope_role, bkey, qkey, limits.now)
                    projected = snap.spent + snap.held + estimate
                    utilization = float(projected / cap) if cap > 0 else 1.0
                    top_utilization = max(top_utilization or 0.0, utilization)
                    if projected > cap:
                        return LedgerAdmission(
                            status=LedgerStatus.BUDGET_EXHAUSTED,
                            budget_state=BudgetState.EXHAUSTED,
                            exhausted_scope=scope,
                            utilization=utilization,
                        )
                    if utilization >= limits.warning_threshold:
                        state = BudgetState.WARNING
            for scope_role, quota in ((None, limits.quota_initiative), (role, limits.quota_role)):
                snap = self._numbers(init, scope_role, bkey, qkey, limits.now)
                rate = self._rate(init, scope_role, limits.now)
                dimension = self._quota_violation(quota, snap, rate, reservation.estimated_tokens)
                if dimension is not None:
                    return LedgerAdmission(
                        status=LedgerStatus.QUOTA_EXHAUSTED,
                        budget_state=state,
                        quota_dimension=dimension,
                    )
            self._revisions[init] = max(limits.budget_revision, self._revisions.get(init, 0))
            if reservation.approval_id is not None:
                self._approvals[reservation.approval_id] = reservation.invocation_ref
            self._reservations[reservation.reservation_id] = reservation
            return LedgerAdmission(
                status=LedgerStatus.ADMITTED,
                budget_state=state,
                reservation=reservation,
                utilization=top_utilization,
            )

    def commit(
        self, reservation_id: str, record: UsageRecord, *, actual_cost, actual_tokens
    ) -> UsageRecord:
        with self._lock:
            reservation = self._reservations[reservation_id]
            if reservation_id in self._records:
                return self._records[reservation_id]  # replay: never double-count
            if reservation.state is ReservationState.RELEASED:
                raise ValueError("a released reservation cannot be committed")
            if actual_cost is None:
                new_state = ReservationState.UNRECONCILED
            else:
                new_state = ReservationState.COMMITTED
            self._reservations[reservation_id] = reservation.model_copy(
                update={
                    "state": new_state,
                    "actual_cost": actual_cost,
                    "actual_tokens": actual_tokens,
                }
            )
            self._records[reservation_id] = record
            self._log.append(record)
            return record

    def release(self, reservation_id: str) -> ReservationState:
        with self._lock:
            reservation = self._reservations[reservation_id]
            if reservation.state is ReservationState.RESERVED:
                self._reservations[reservation_id] = reservation.model_copy(
                    update={"state": ReservationState.RELEASED}
                )
                return ReservationState.RELEASED
            return reservation.state

    def reconcile(
        self, reservation_id: str, record: UsageRecord, *, actual_cost, actual_tokens
    ) -> UsageRecord:
        with self._lock:
            reservation = self._reservations[reservation_id]
            if reservation.state is not ReservationState.UNRECONCILED:
                return self._records.get(reservation_id, record)  # already settled: no change
            if actual_cost is None and record.status.value != "released":
                raise ValueError("reconciliation needs an authoritative cost or a release")
            new_state = (
                ReservationState.RELEASED if actual_cost is None else ReservationState.COMMITTED
            )
            self._reservations[reservation_id] = reservation.model_copy(
                update={
                    "state": new_state,
                    "actual_cost": actual_cost,
                    "actual_tokens": actual_tokens,
                }
            )
            self._records[reservation_id] = record
            self._log.append(record)
            return record

    def get(self, reservation_id: str) -> Reservation | None:
        with self._lock:
            return self._reservations.get(reservation_id)

    def snapshot(self, initiative_id, role, budget_period_key, quota_period_key) -> LedgerSnapshot:
        from datetime import UTC, datetime

        with self._lock:
            return self._numbers(
                initiative_id, role, budget_period_key, quota_period_key, datetime.now(UTC)
            )

    def usage_records(self, initiative_id: str) -> tuple[UsageRecord, ...]:
        with self._lock:
            return tuple(r for r in self._log if r.initiative_id == initiative_id)
