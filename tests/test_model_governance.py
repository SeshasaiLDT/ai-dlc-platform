"""Offline tests for model cost, quota and fallback controls (AIDLC-52)."""

from __future__ import annotations

import ast
import asyncio
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_deterministic_routing import Env as RoutingEnv
from test_deterministic_routing import FakeTelemetry
from test_model_registry import ADMIN
from test_model_registry import Env as RegistryEnv
from test_model_registry import sel as base_selection

import ai_dlc.application.model_governance as gov_pkg
from ai_dlc.adapters.model_governance import InMemoryGovernanceLedger
from ai_dlc.application.agent_harness import (
    AgentContext,
    ApprovalReference,
    DataClassification,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    ModelRole,
    ReasoningLevel,
    RoleProfiles,
)
from ai_dlc.application.model_governance import (
    NEVER_FALLBACK,
    AccountingStatus,
    AdmissionRequest,
    AdmissionStatus,
    BudgetState,
    FallbackPlanner,
    GovernedModelClient,
    GovernedOutcome,
    ModelFailure,
    ModelGovernanceService,
    Reservation,
    ReservationState,
    ReviewerConstraints,
    TokenUsage,
    UsageSource,
    calculate_cost,
    classify_model_failure,
    estimate_cost,
    fallback_permitted,
)
from ai_dlc.application.model_registry import (
    DeploymentCapabilities,
    DeploymentContext,
    DeploymentGovernance,
    LatencyMetadata,
    OperationalAvailability,
    PricingMetadata,
)
from ai_dlc.application.review import ModelProvenance, ReviewRequirements
from ai_dlc.domain.approval import ApprovalStatus
from ai_dlc.domain.initiative.enums import BudgetAction, FallbackFailure, FallbackOrdering
from ai_dlc.domain.initiative.enums import ModelRole as ProfileRole
from ai_dlc.domain.initiative.models import (
    BudgetPolicy,
    DeploymentPriority,
    FallbackPolicy,
    ModelGovernancePolicy,
    QuotaLimits,
    QuotaPolicy,
    RoleAmount,
    RoleQuota,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
PROFILES = RoleProfiles.defaults()


def _no_pricing_required() -> RoleProfiles:
    base = RoleProfiles.defaults()
    standard = base.for_role(ModelRole.STANDARD_REASONING)
    relaxed = standard.model_copy(
        update={"cost": standard.cost.model_copy(update={"require_pricing_metadata": False})}
    )
    return RoleProfiles(profiles={**base.profiles, ModelRole.STANDARD_REASONING: relaxed})


UNPRICED_OK = _no_pricing_required()  # lets deployments without prices be *eligible*
TOKENS_IN, TOKENS_OUT = 100_000, 20_000  # at 3/15 per million tokens this estimates 0.600000
COST = Decimal("0.600000")
D = Decimal


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeApprovals:
    def __init__(self, statuses: dict[str, ApprovalStatus] | None = None) -> None:
        self.statuses = statuses or {}

    async def request_approval(self, intent, *, context):
        raise AssertionError("the governance layer must not request approvals itself")

    async def get_approval_status(self, approval_id, *, context):
        return ApprovalReference(
            approval_id=approval_id, status=self.statuses.get(approval_id, ApprovalStatus.PENDING)
        )


def ok(usage=None, **extra) -> ExecutionResult:
    metadata = {"usage": usage} if usage is not None else None
    return ExecutionResult(
        status=ExecutionStatus.SUCCEEDED,
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        output={"text": "x"},
        metadata=metadata,
    )


def fail(code: str, status=ExecutionStatus.FAILED, usage=None) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        error=ExecutionError(code=code, message="provider error"),
        metadata={"usage": usage} if usage else None,
    )


class FakeProvider:
    """Scripted ModelProvider: results per deployment ID; records every call."""

    def __init__(self, script: dict[str, list[ExecutionResult]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, str]] = []

    async def invoke_model(self, role: str, request: Invocation, *, context: AgentContext):
        deployment = request.metadata["deployment_id"]
        self.calls.append((role, deployment))
        queue = self.script.get(deployment) or [ok({"input_tokens": 1000, "output_tokens": 200})]
        return queue.pop(0) if len(queue) > 1 else queue[0]


USAGE = {"input_tokens": 1000, "output_tokens": 200}


def policy(*, budget=None, quota=None, fallback=None) -> ModelGovernancePolicy:
    return ModelGovernancePolicy(
        budget=budget or BudgetPolicy(),
        quota=quota or QuotaPolicy(),
        fallback=fallback or FallbackPolicy(),
    )


class World:
    def __init__(
        self, *, controls=None, telemetry=None, approvals=None, deployments=None, profiles=None
    ) -> None:
        self.reg = RegistryEnv()
        for name, overrides in (deployments or {"dep-a": {}}).items():
            self.reg.add(name, **overrides)
        env = RoutingEnv()
        self.context = env.context
        self.profile = env.profile.model_copy(update={"inference_controls": controls or policy()})
        self.clock = Clock()
        self.telemetry = telemetry or FakeTelemetry()
        self.ledger = InMemoryGovernanceLedger()
        self.approvals = approvals or FakeApprovals()
        self.service = ModelGovernanceService(
            self.ledger,
            self.reg.reader,
            profiles or PROFILES,
            approvals=self.approvals,
            telemetry=self.telemetry,
            clock=self.clock,
        )
        self.planner = FallbackPlanner(self.reg.reader, profiles or PROFILES)

    def request(self, deployment="dep-a", ref="call-1", **kw) -> AdmissionRequest:
        kw.setdefault("selection", base_selection())
        current = self.reg.reader.get(deployment)
        return AdmissionRequest(
            deployment_id=deployment,
            evaluated_revision=kw.pop("evaluated_revision", current.revision),
            estimated_input_tokens=kw.pop("tokens_in", TOKENS_IN),
            max_output_tokens=kw.pop("tokens_out", TOKENS_OUT),
            invocation_ref=ref,
            **kw,
        )

    def admit(self, request=None, **kw):
        return asyncio.run(
            self.service.admit(
                request or self.request(**kw),
                context=self.context,
                profile=self.profile,
                initiative_revision=3,
            )
        )

    def client(self, provider, **kw):
        return GovernedModelClient(
            provider, self.service, self.planner, telemetry=self.telemetry, **kw
        )

    def invoke(self, provider, *, selection=None, ref="call-1", **kw):
        client = kw.pop("client", None) or self.client(provider)
        return asyncio.run(
            client.invoke(
                selection or base_selection(),
                Invocation(input={"prompt": "hello"}),
                context=self.context,
                profile=self.profile,
                initiative_revision=3,
                estimated_input_tokens=kw.pop("tokens_in", TOKENS_IN),
                max_output_tokens=kw.pop("tokens_out", TOKENS_OUT),
                invocation_ref=ref,
                **kw,
            )
        )

    def snapshot(self, role=None):
        return self.ledger.snapshot("travel-platform", role, "2026-10", "2026-10-10")


# --- cost and usage ---------------------------------------------------------------------------


def test_cost_calculation_is_decimal_rounded_up_and_deterministic() -> None:
    pricing = PricingMetadata(
        input_cost_per_million_tokens=D("3"), output_cost_per_million_tokens=D("15")
    )
    assert calculate_cost(TokenUsage(input_tokens=1000, output_tokens=200), pricing, "USD") == D(
        "0.006000"
    )
    odd = PricingMetadata(
        input_cost_per_million_tokens=D("0.0000007"), output_cost_per_million_tokens=D("0")
    )
    cost = calculate_cost(TokenUsage(input_tokens=1, output_tokens=0), odd, "USD")
    assert cost == D("0.000001") and isinstance(cost, Decimal)  # rounds up, never under-counts
    assert estimate_cost(TOKENS_IN, TOKENS_OUT, pricing, "USD") == COST
    assert estimate_cost(TOKENS_IN, TOKENS_OUT, pricing, "USD", attempts=2) == D("1.200000")


def test_unknown_pricing_and_currency_mismatch_give_unknown_cost_not_zero() -> None:
    usage = TokenUsage(input_tokens=10, output_tokens=10)
    assert calculate_cost(usage, PricingMetadata(), "USD") is None
    priced = PricingMetadata(
        input_cost_per_million_tokens=D("1"), output_cost_per_million_tokens=D("1"), currency="EUR"
    )
    assert calculate_cost(usage, priced, "USD") is None
    assert (
        estimate_cost(1, 1, priced, "USD") is None
        and estimate_cost(1, 1, PricingMetadata(), "USD") is None
    )


def test_cached_tokens_only_priced_when_metadata_supports_it() -> None:
    base = dict(input_cost_per_million_tokens=D("3"), output_cost_per_million_tokens=D("15"))
    usage = TokenUsage(input_tokens=1000, output_tokens=0, cache_read_tokens=1_000_000)
    assert (
        calculate_cost(usage, PricingMetadata(**base), "USD") is None
    )  # not silently input-priced
    cached = PricingMetadata(**base, cache_read_cost_per_million_tokens=D("0.3"))
    assert calculate_cost(usage, cached, "USD") == D("0.303000")  # no double counting
    zero_cache = TokenUsage(input_tokens=1000, output_tokens=0, cache_read_tokens=0)
    assert calculate_cost(zero_cache, PricingMetadata(**base), "USD") == D("0.003000")
    other = TokenUsage(input_tokens=1, output_tokens=1, other={"reasoning_tokens": 5})
    assert calculate_cost(other, PricingMetadata(**base), "USD") is None


def test_invalid_usage_and_money_rejected() -> None:
    for bad in (
        {"input_tokens": -1, "output_tokens": 0},
        {"input_tokens": 0, "output_tokens": -5},
        {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": -1},
        {"input_tokens": 0, "output_tokens": 0, "other": {"Bad Key": 1}},
        {"input_tokens": 0, "output_tokens": 0, "other": {"k": -1}},
        {"input_tokens": 10**12, "output_tokens": 0},
        {"input_tokens": 0, "output_tokens": 0, "surprise": 1},
    ):
        with pytest.raises(ValidationError):
            TokenUsage(**bad)
    for price in (D("-1"), D("NaN"), D("Infinity")):
        with pytest.raises(ValidationError):
            PricingMetadata(input_cost_per_million_tokens=price)
    with pytest.raises(ValidationError):
        PricingMetadata(currency="usd")
    with pytest.raises(ValidationError):
        BudgetPolicy(initiative_limit=D("-1"))
    with pytest.raises(ValidationError):
        BudgetPolicy(initiative_limit=D("NaN"))
    with pytest.raises(ValidationError):
        BudgetPolicy(currency="dollars")


# --- budget admission -------------------------------------------------------------------------


def test_per_role_budget_admission_and_exhaustion() -> None:
    budget = BudgetPolicy(
        role_limits=(RoleAmount(role=ProfileRole.STANDARD_REASONING, amount=D("1.0")),)
    )
    w = World(controls=policy(budget=budget))
    first = w.admit(ref="c1")
    assert first.status is AdmissionStatus.ADMITTED and first.estimated_cost == COST
    assert first.budget_state is BudgetState.AVAILABLE
    second = w.admit(ref="c2")  # 0.6 + 0.6 > 1.0
    assert second.status is AdmissionStatus.BLOCKED_BUDGET
    assert second.budget_state is BudgetState.EXHAUSTED and second.reservation_id is None
    assert "model.budget_exhausted" in w.telemetry.events
    # a different role has no budget configured, so it is not constrained by this one
    other = w.admit(ref="c3", selection=base_selection(ModelRole.DEEP_REASONING))
    assert other.status is AdmissionStatus.ADMITTED


def test_initiative_wide_budget_and_warning_threshold() -> None:
    budget = BudgetPolicy(initiative_limit=D("1.0"), warning_threshold=0.5)
    w = World(controls=policy(budget=budget))
    first = w.admit(ref="c1")
    assert first.status is AdmissionStatus.ADMITTED and first.budget_state is BudgetState.WARNING
    assert first.utilization == pytest.approx(0.6)
    assert w.admit(ref="c2").status is AdmissionStatus.BLOCKED_BUDGET
    assert w.snapshot().held == COST


def test_request_limit_can_only_lower_the_organizational_limit() -> None:
    w = World(controls=policy(budget=BudgetPolicy(request_limit=D("1.0"))))
    assert w.admit(ref="c1", request_cost_limit=D("100")).status is AdmissionStatus.ADMITTED
    assert w.admit(ref="c2", request_cost_limit=D("0.5")).status is AdmissionStatus.BLOCKED_BUDGET
    big = World(controls=policy(budget=BudgetPolicy(request_limit=D("0.5"))))
    assert big.admit(request_cost_limit=D("100")).status is AdmissionStatus.BLOCKED_BUDGET


def test_unknown_pricing_is_never_free_when_a_budget_applies() -> None:
    w = World(
        controls=policy(budget=BudgetPolicy(initiative_limit=D("5"))),
        deployments={"dep-a": {"pricing": PricingMetadata()}},
        profiles=UNPRICED_OK,
    )
    d = w.admit()
    assert d.status is AdmissionStatus.BLOCKED_BUDGET and d.reason == "cost_unknown"
    assert d.budget_state is BudgetState.UNKNOWN and d.estimated_cost is None
    free = World(deployments={"dep-a": {"pricing": PricingMetadata()}}, profiles=UNPRICED_OK)
    assert free.admit().status is AdmissionStatus.ADMITTED


def test_budget_breach_follows_configured_action() -> None:
    allow = World(
        controls=policy(
            budget=BudgetPolicy(initiative_limit=D("0.1"), on_exhausted=BudgetAction.ALLOW)
        )
    )
    assert allow.admit().status is AdmissionStatus.ADMITTED  # audit-only
    assert "model.budget_exhausted" in allow.telemetry.events


def test_approval_required_policy_uses_existing_approval_contracts() -> None:
    budget = BudgetPolicy(initiative_limit=D("0.1"), on_exhausted=BudgetAction.REQUIRE_APPROVAL)
    approvals = FakeApprovals(
        {"appr-ok": ApprovalStatus.APPROVED, "appr-no": ApprovalStatus.REJECTED}
    )
    w = World(controls=policy(budget=budget), approvals=approvals)
    pending = w.admit(ref="c1")
    assert pending.status is AdmissionStatus.APPROVAL_REQUIRED and pending.reservation_id is None
    intent = pending.approval_intent
    assert intent.operation == "model.budget_exception"
    assert intent.logical_target["invocation_ref"] == "c1"
    assert intent.request_key.startswith("budget-") and w.snapshot().held == 0  # nothing held
    assert w.admit(ref="c1").approval_intent.request_key == intent.request_key  # deterministic
    for bad in ("appr-no", "appr-unknown"):
        assert w.admit(ref="c1", approval_id=bad).status is AdmissionStatus.APPROVAL_INVALID
    approved = w.admit(ref="c1", approval_id="appr-ok")
    assert approved.status is AdmissionStatus.ADMITTED and approved.reservation_id
    # single use: the same approval cannot cover another invocation
    assert w.admit(ref="c2", approval_id="appr-ok").status is AdmissionStatus.APPROVAL_INVALID
    # no approval provider configured: nothing can be approved
    bare = World(controls=policy(budget=budget))
    bare.service = ModelGovernanceService(bare.ledger, bare.reg.reader, PROFILES, clock=bare.clock)
    assert bare.admit(approval_id="appr-ok").status is AdmissionStatus.APPROVAL_INVALID


def test_approved_exception_does_not_waive_quotas() -> None:
    budget = BudgetPolicy(initiative_limit=D("0.1"), on_exhausted=BudgetAction.REQUIRE_APPROVAL)
    quota = QuotaPolicy(initiative=QuotaLimits(max_invocations=0))
    w = World(
        controls=policy(budget=budget, quota=quota),
        approvals=FakeApprovals({"a": ApprovalStatus.APPROVED}),
    )
    assert w.admit(approval_id="a").status is AdmissionStatus.BLOCKED_QUOTA


def test_concurrent_reservations_cannot_spend_the_same_budget() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("1.0"))))
    results: list[AdmissionStatus] = []
    barrier = threading.Barrier(8)

    def work(i: int) -> None:
        barrier.wait()
        results.append(w.admit(ref=f"c{i}").status)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(AdmissionStatus.ADMITTED) == 1  # 0.6 fits once in 1.0
    assert w.snapshot().held == COST


def test_stale_budget_revision_and_duplicate_reservation() -> None:
    new = BudgetPolicy(initiative_limit=D("5"), revision=2)
    w = World(controls=policy(budget=new))
    assert w.admit(ref="c1").status is AdmissionStatus.ADMITTED
    w.profile = w.profile.model_copy(
        update={
            "inference_controls": policy(budget=BudgetPolicy(initiative_limit=D("5"), revision=1))
        }
    )
    stale = w.admit(ref="c2")
    assert stale.status is AdmissionStatus.STALE_BUDGET_REVISION and stale.reservation_id is None
    w.profile = w.profile.model_copy(update={"inference_controls": policy(budget=new)})
    again = w.admit(ref="c1")  # same stable idempotency identifier
    assert again.status is AdmissionStatus.DUPLICATE and w.snapshot().held == COST


# --- accounting lifecycle ---------------------------------------------------------------------


def test_reservation_commit_replaces_estimate_with_actual_and_never_double_counts() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("5"))))
    rid = w.admit().reservation_id
    usage = TokenUsage(input_tokens=1000, output_tokens=200)
    record = w.service.record_usage(rid, usage, context=w.context)
    assert record.status is AccountingStatus.COMMITTED and record.actual_cost == D("0.006000")
    assert record.source is UsageSource.PROVIDER_REPORTED and record.estimated_cost == COST
    assert record.currency == "USD" and record.recorded_at == NOW
    assert (record.request_id, record.correlation_id, record.task_id) == (
        "req-1",
        "corr-1",
        "task-1",
    )
    snap = w.snapshot()
    assert snap.spent == D("0.006000") and snap.held == 0
    again = w.service.record_usage(
        rid, TokenUsage(input_tokens=9, output_tokens=9), context=w.context
    )
    assert again == record and w.snapshot().spent == D("0.006000")  # replay is a no-op
    assert len(w.ledger.usage_records("travel-platform")) == 1
    assert w.ledger.get(rid).state is ReservationState.COMMITTED


def test_release_frees_the_hold() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("1.0"))))
    rid = w.admit(ref="c1").reservation_id
    assert w.admit(ref="c2").status is AdmissionStatus.BLOCKED_BUDGET
    w.service.release(rid, context=w.context)
    assert w.snapshot().held == 0 and w.ledger.get(rid).state is ReservationState.RELEASED
    assert w.admit(ref="c2").status is AdmissionStatus.ADMITTED
    with pytest.raises(ValueError):
        w.service.record_usage(rid, None, context=w.context)  # a released call cannot be committed
    with pytest.raises(ValueError):
        w.service.release("res-unknown", context=w.context)


def test_missing_usage_is_unknown_not_zero_and_keeps_the_estimate_held() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("5"))))
    rid = w.admit().reservation_id
    record = w.service.record_usage(rid, None, context=w.context)
    assert record.usage is None and record.source is UsageSource.MISSING
    assert record.status is AccountingStatus.UNKNOWN_COST and record.actual_cost is None
    snap = w.snapshot()
    assert snap.spent == 0 and snap.held == COST and snap.unreconciled == 1
    assert w.ledger.get(rid).state is ReservationState.UNRECONCILED
    assert ("model.accounting.unknown", 1.0) in w.telemetry.metrics


def test_unknown_pricing_after_call_leaves_cost_unknown() -> None:
    w = World(deployments={"dep-a": {"pricing": PricingMetadata()}}, profiles=UNPRICED_OK)
    rid = w.admit().reservation_id
    record = w.service.record_usage(
        rid, TokenUsage(input_tokens=5, output_tokens=5), context=w.context
    )
    assert record.status is AccountingStatus.UNKNOWN_COST and record.actual_cost is None
    assert record.usage.input_tokens == 5


def test_ambiguous_outcome_is_not_refunded_and_is_reconciled_explicitly() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("5"))))
    rid = w.admit().reservation_id
    w.service.record_ambiguous(rid, context=w.context)
    assert w.snapshot().held == COST and "model.reconciliation_required" in w.telemetry.events
    with pytest.raises(ValueError):
        w.service.reconcile(rid, executed=True, context=w.context)  # needs usage and a cost
    done = w.service.reconcile(
        rid,
        executed=True,
        usage=TokenUsage(input_tokens=1000, output_tokens=200),
        billed_cost=D("0.007"),
        context=w.context,
    )
    assert done.status is AccountingStatus.COMMITTED and done.actual_cost == D("0.007")
    assert w.snapshot().spent == D("0.007") and w.snapshot().held == 0
    assert w.service.reconcile(rid, executed=False, context=w.context) == done  # idempotent
    rid2 = w.admit(ref="c2").reservation_id
    w.service.record_ambiguous(rid2, context=w.context)
    released = w.service.reconcile(rid2, executed=False, context=w.context)
    assert released.status is AccountingStatus.RELEASED and w.snapshot().held == 0


def test_period_keys_and_initiative_isolation() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("1.0"))))
    assert w.admit(ref="c1").status is AdmissionStatus.ADMITTED
    foreign = Reservation(
        reservation_id="res-foreign",
        initiative_id="field-operations",
        role=ModelRole.STANDARD_REASONING,
        deployment_id="dep-a",
        registry_revision=1,
        invocation_ref="x",
        estimated_cost=D("5"),
        estimated_tokens=1,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        budget_period_key="2026-10",
        quota_period_key="2026-10-10",
        budget_revision=1,
    )
    from ai_dlc.application.model_governance import AdmissionLimits

    limits = AdmissionLimits(currency="USD", budget_revision=1, now=NOW)
    assert w.ledger.reserve(foreign, limits).status.value == "admitted"
    assert w.snapshot().held == COST  # the other initiative's hold is not ours
    w.clock.now = NOW + timedelta(days=40)  # next month: a fresh period
    assert w.admit(ref="c2").status is AdmissionStatus.ADMITTED
    mismatched = w.profile.model_copy(
        update={"initiative": w.profile.initiative.model_copy(update={"id": "other-initiative"})}
    )
    d = asyncio.run(
        w.service.admit(
            w.request(ref="c9"), context=w.context, profile=mismatched, initiative_revision=3
        )
    )
    assert d.status is AdmissionStatus.INITIATIVE_MISMATCH


# --- quotas -----------------------------------------------------------------------------------


def test_invocation_and_token_quotas_are_distinct_from_budget() -> None:
    w = World(controls=policy(quota=QuotaPolicy(initiative=QuotaLimits(max_invocations=2))))
    assert w.admit(ref="c1").status is AdmissionStatus.ADMITTED
    assert w.admit(ref="c2").status is AdmissionStatus.ADMITTED
    third = w.admit(ref="c3")
    assert third.status is AdmissionStatus.BLOCKED_QUOTA and third.quota_dimension == "invocations"
    assert "model.quota_exhausted" in w.telemetry.events
    tokens = World(controls=policy(quota=QuotaPolicy(initiative=QuotaLimits(max_tokens=150_000))))
    assert tokens.admit(ref="c1").status is AdmissionStatus.ADMITTED  # 120k
    blocked = tokens.admit(ref="c2")
    assert blocked.status is AdmissionStatus.BLOCKED_QUOTA and blocked.quota_dimension == "tokens"


def test_concurrency_quota_and_slot_release() -> None:
    w = World(controls=policy(quota=QuotaPolicy(initiative=QuotaLimits(max_concurrent=1))))
    first = w.admit(ref="c1")
    blocked = w.admit(ref="c2")
    assert blocked.quota_dimension == "concurrent"
    w.service.record_usage(
        first.reservation_id, TokenUsage(input_tokens=1, output_tokens=1), context=w.context
    )
    assert w.admit(ref="c2").status is AdmissionStatus.ADMITTED  # committed -> slot freed
    rid = w.admit(ref="c2").reservation_id
    w.service.release(rid, context=w.context)
    assert w.admit(ref="c3").status is AdmissionStatus.ADMITTED  # released -> slot freed


def test_abandoned_reservation_frees_slot_but_keeps_money_held() -> None:
    controls = policy(
        budget=BudgetPolicy(initiative_limit=D("5")),
        quota=QuotaPolicy(initiative=QuotaLimits(max_concurrent=1)),
    )
    w = World(controls=controls)
    rid = w.admit(ref="c1").reservation_id
    assert w.admit(ref="c2").quota_dimension == "concurrent"
    w.clock.now = NOW + timedelta(minutes=11)  # past the reservation TTL
    assert w.admit(ref="c2").status is AdmissionStatus.ADMITTED
    assert w.ledger.get(rid).state is ReservationState.UNRECONCILED  # needs reconciliation
    assert w.snapshot().held >= COST


def test_role_quota_and_rate_limit() -> None:
    quota = QuotaPolicy(
        role_limits=(
            RoleQuota(
                role=ProfileRole.STANDARD_REASONING, limits=QuotaLimits(max_requests_per_minute=1)
            ),
        )
    )
    w = World(controls=policy(quota=quota))
    assert w.admit(ref="c1").status is AdmissionStatus.ADMITTED
    limited = w.admit(ref="c2")
    assert limited.status is AdmissionStatus.BLOCKED_QUOTA and limited.quota_dimension == "rate"
    deep = w.admit(ref="c3", selection=base_selection(ModelRole.DEEP_REASONING))
    assert deep.status is AdmissionStatus.ADMITTED  # other role unaffected
    w.clock.now = NOW + timedelta(seconds=61)
    assert w.admit(ref="c4").status is AdmissionStatus.ADMITTED


# --- registry checks before dispatch ----------------------------------------------------------


def test_registry_changes_between_evaluation_and_dispatch_are_caught() -> None:
    w = World()
    stale = w.request()
    rec = w.reg.reader.get("dep-a")
    w.reg.admin.update_metadata(
        ADMIN,
        rec.spec.model_copy(update={"region": "region-b"}),
        expected_revision=rec.revision,
        correlation_id="c",
    )
    changed = w.admit(stale)
    assert changed.status is AdmissionStatus.REGISTRY_CHANGED and changed.reservation_id is None
    fresh = w.admit(w.request())
    assert fresh.status is AdmissionStatus.ADMITTED
    current = w.reg.reader.get("dep-a")
    w.reg.admin.disable(ADMIN, "dep-a", expected_revision=current.revision, correlation_id="c")
    gone = w.admit(w.request(ref="c2", evaluated_revision=current.revision + 1))
    assert gone.status is AdmissionStatus.DEPLOYMENT_UNAVAILABLE
    assert "model.deployment_unavailable" in w.telemetry.events
    assert w.admit(w.request(deployment="dep-a", ref="c3", evaluated_revision=1)).status is (
        AdmissionStatus.DEPLOYMENT_UNAVAILABLE
    )


# --- fallback planning ------------------------------------------------------------------------


def three(**per) -> dict:
    return {
        "dep-a": {"provider_id": "prov-a", **per.get("a", {})},
        "dep-b": {"provider_id": "prov-b", **per.get("b", {})},
        "dep-c": {"provider_id": "prov-c", **per.get("c", {})},
    }


def ids(plan) -> list[str]:
    return [m.deployment_id for m in plan.candidates]


def plan(w, **kw):
    fb = kw.pop("fallback", FallbackPolicy(enabled=True))
    return w.planner.plan(
        kw.pop("selection", base_selection()),
        fb,
        estimated_input_tokens=TOKENS_IN,
        max_output_tokens=TOKENS_OUT,
        **kw,
    )


def test_ordering_is_deterministic_configurable_and_tie_broken_by_id() -> None:
    w = World(deployments=three())
    assert ids(plan(w)) == ["dep-a", "dep-b", "dep-c"]  # all tie on every key -> ID order
    first = plan(
        w,
        fallback=FallbackPolicy(
            enabled=True,
            priorities=(
                DeploymentPriority(deployment_id="dep-c", priority=1),
                DeploymentPriority(deployment_id="dep-b", priority=5),
            ),
        ),
    )
    assert ids(first) == ["dep-c", "dep-b", "dep-a"]  # unlisted ranks last
    assert ids(plan(w)) == ids(plan(w))


def test_cost_and_latency_ordering_treat_unknowns_as_worst() -> None:
    cheap = PricingMetadata(
        input_cost_per_million_tokens=D("1"), output_cost_per_million_tokens=D("1")
    )
    dear = PricingMetadata(
        input_cost_per_million_tokens=D("9"), output_cost_per_million_tokens=D("9")
    )
    w = World(
        deployments=three(
            a={"pricing": PricingMetadata()}, b={"pricing": dear}, c={"pricing": cheap}
        ),
        profiles=UNPRICED_OK,
    )
    by_cost = plan(w, fallback=FallbackPolicy(enabled=True, ordering=(FallbackOrdering.COST,)))
    assert ids(by_cost) == ["dep-c", "dep-b", "dep-a"]  # unknown price is never free
    w2 = World(
        deployments=three(
            a={"latency": LatencyMetadata()},
            b={"latency": LatencyMetadata(typical_latency_ms=900)},
            c={"latency": LatencyMetadata(typical_latency_ms=100)},
        )
    )
    by_latency = plan(
        w2, fallback=FallbackPolicy(enabled=True, ordering=(FallbackOrdering.LATENCY,))
    )
    assert ids(by_latency) == ["dep-c", "dep-b", "dep-a"]  # unknown latency is never optimal


def test_fallback_candidates_must_satisfy_the_original_requirements() -> None:
    w = World(
        deployments=three(
            a={"enable": False},
            b={
                "capabilities": DeploymentCapabilities(
                    structured_output=False, tool_calling=False, reasoning=ReasoningLevel.BASIC
                )
            },
            c={},
        )
    )
    found = plan(w)
    assert ids(found) == ["dep-c"]
    rejected = dict(found.rejected)
    assert "disabled" in rejected["dep-a"] and "reasoning_level_insufficient" in rejected["dep-b"]
    down = World(deployments=three())
    rec = down.reg.reader.get("dep-b")
    down.reg.admin.set_availability(
        ADMIN,
        "dep-b",
        OperationalAvailability.UNAVAILABLE,
        expected_revision=rec.revision,
        correlation_id="c",
    )
    assert ids(plan(down)) == ["dep-a", "dep-c"]


def test_fallback_respects_data_provider_region_context_and_capability_rules() -> None:
    w = World(
        deployments=three(
            a={
                "governance": DeploymentGovernance(
                    supported_classifications=frozenset({DataClassification.PUBLIC}),
                    eligible_roles=frozenset(ModelRole),
                )
            },
            b={"region": "region-z"},
            c={"context": DeploymentContext(max_context_tokens=40_000, max_output_tokens=5_000)},
        )
    )
    restricted = base_selection(data_classification=DataClassification.INTERNAL)
    assert "dep-a" not in ids(plan(w, selection=restricted))
    assert ids(plan(w, selection=base_selection(allowed_providers=frozenset({"prov-b"})))) == [
        "dep-b"
    ]
    assert ids(
        plan(w, selection=base_selection(denied_providers=frozenset({"prov-a", "prov-b"})))
    ) == ["dep-c"]
    assert "dep-b" not in ids(
        plan(w, selection=base_selection(allowed_regions=frozenset({"region-a"})))
    )
    big = plan(
        w,
        selection=base_selection(requested_context_tokens=100_000, requested_output_tokens=10_000),
    )
    assert "dep-c" not in ids(big)  # context and output capacity
    tools = plan(
        w,
        selection=base_selection(
            required_capabilities=__import__(
                "ai_dlc.application.agent_harness", fromlist=["CapabilityRequirements"]
            ).CapabilityRequirements(tool_calling=True)
        ),
    )
    assert ids(tools) == ["dep-b", "dep-c"]  # dep-a only supports public data
    no_tools = World(
        deployments=three(
            b={
                "capabilities": DeploymentCapabilities(
                    structured_output=True, tool_calling=False, reasoning=ReasoningLevel.EXTENDED
                )
            }
        )
    )
    needs = base_selection(
        required_capabilities=__import__(
            "ai_dlc.application.agent_harness", fromlist=["CapabilityRequirements"]
        ).CapabilityRequirements(tool_calling=True)
    )
    assert "dep-b" not in ids(plan(no_tools, selection=needs))


def test_role_is_never_changed_so_deep_never_downgrades_to_standard() -> None:
    standard_only = DeploymentGovernance(
        supported_classifications=frozenset(DataClassification),
        eligible_roles=frozenset({ModelRole.STANDARD_REASONING}),
    )
    w = World(
        deployments=three(
            a={"governance": standard_only},
            b={"governance": standard_only},
            c={"governance": standard_only},
        )
    )
    deep = plan(w, selection=base_selection(ModelRole.DEEP_REASONING))
    assert deep.candidates == ()  # no silent downgrade
    assert all("role_not_declared" in reasons for _, reasons in deep.rejected)
    assert ids(plan(w)) == ["dep-a", "dep-b", "dep-c"]


def test_reviewer_fallback_preserves_independence() -> None:
    w = World(
        deployments={
            "gen-a": {
                "provider_id": "prov-a",
                "model_identifier": "m-gen",
                "model_family": "fam-a",
            },
            "rev-same-family": {
                "provider_id": "prov-b",
                "model_identifier": "m-1",
                "model_family": "fam-a",
            },
            "rev-clean": {
                "provider_id": "prov-b",
                "model_identifier": "m-2",
                "model_family": "fam-b",
            },
        }
    )
    generator = ModelProvenance.from_registry(
        w.reg.reader.get("gen-a"), role=ModelRole.STANDARD_REASONING, execution_ref="exec-1"
    )
    req = ReviewerConstraints(generator, ReviewRequirements(True, False, True, False, False))
    selection = base_selection(ModelRole.INDEPENDENT_REVIEWER)
    found = plan(w, selection=selection, reviewer=req)
    assert ids(found) == ["rev-clean"]
    rejected = dict(found.rejected)
    assert "same_family" in rejected["rev-same-family"]
    assert "not_separate_from_generator" in rejected["gen-a"]
    with pytest.raises(ValueError):
        plan(w, selection=selection)  # reviewer planning needs independence constraints


def test_attempted_deployments_are_excluded() -> None:
    w = World(deployments=three())
    found = plan(w, exclude=frozenset({"dep-a"}))
    assert ids(found) == ["dep-b", "dep-c"] and dict(found.rejected)["dep-a"] == (
        "already_attempted",
    )


def test_failure_classification_and_fallback_eligibility() -> None:
    cases = {
        "provider_unavailable": ModelFailure.PROVIDER_UNAVAILABLE,
        "rate_limited": ModelFailure.THROTTLED,
        "throttled": ModelFailure.THROTTLED,
        "context_length_exceeded": ModelFailure.CONTEXT_CAPACITY,
        "unauthenticated": ModelFailure.AUTHENTICATION,
        "unauthorized_operation": ModelFailure.AUTHORIZATION,
        "permission_denied": ModelFailure.AUTHORIZATION,
        "policy_violation": ModelFailure.POLICY_VIOLATION,
        "approval_required": ModelFailure.POLICY_VIOLATION,
        "schema_violation": ModelFailure.INVALID_OUTPUT,
        "model_timeout": ModelFailure.TIMEOUT,
        "something_odd": ModelFailure.PERMANENT_MODEL_ERROR,
    }
    for code, expected in cases.items():
        assert classify_model_failure(fail(code)) is expected, code
    assert classify_model_failure(fail("x", ExecutionStatus.CANCELLED)) is ModelFailure.CANCELLED
    assert classify_model_failure(fail("x", ExecutionStatus.TIMED_OUT)) is ModelFailure.TIMEOUT
    with pytest.raises(ValueError):
        classify_model_failure(ok())
    everything = FallbackPolicy(enabled=True, failure_categories=tuple(FallbackFailure))
    for failure in NEVER_FALLBACK:  # no configuration can enable these
        assert not fallback_permitted(failure, everything)
    assert not fallback_permitted(ModelFailure.THROTTLED, FallbackPolicy(enabled=False))
    default = FallbackPolicy(enabled=True)
    assert fallback_permitted(ModelFailure.TRANSIENT, default)
    assert fallback_permitted(ModelFailure.THROTTLED, default)
    assert not fallback_permitted(ModelFailure.PERMANENT_MODEL_ERROR, default)
    assert not fallback_permitted(ModelFailure.INVALID_OUTPUT, default)


# --- governed execution -----------------------------------------------------------------------


def fb(**kw) -> ModelGovernancePolicy:
    return policy(fallback=FallbackPolicy(enabled=True, **kw))


def test_success_records_usage_and_cost_and_dispatches_by_role() -> None:
    w = World(controls=fb(), deployments=three())
    provider = FakeProvider()
    out = w.invoke(provider, selection=base_selection(ModelRole.STANDARD_REASONING))
    assert out.outcome is GovernedOutcome.SUCCEEDED and out.result.output == {"text": "x"}
    assert provider.calls == [("standard_reasoning", "dep-a")]
    (record,) = out.usage
    assert record.status is AccountingStatus.COMMITTED and record.actual_cost == D("0.006000")
    assert out.attempts[0].deployment_id == "dep-a" and out.attempts[0].reservation_id
    assert ("model.cost.recorded", 0.006) in w.telemetry.metrics
    assert (
        "model.usage_recorded" in w.telemetry.events
        and "model.cost_calculated" in w.telemetry.events
    )


def test_malformed_provider_usage_is_treated_as_missing() -> None:
    w = World(deployments=three())
    provider = FakeProvider({"dep-a": [ok({"input_tokens": -4, "output_tokens": 1})]})
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.SUCCEEDED
    assert out.usage[0].source is UsageSource.MISSING and out.usage[0].actual_cost is None
    no_usage = FakeProvider({"dep-a": [ok()]})
    assert (
        World(deployments=three()).invoke(no_usage).usage[0].status is AccountingStatus.UNKNOWN_COST
    )


@pytest.mark.parametrize("code", ["provider_unavailable", "rate_limited"])
def test_transient_and_throttled_failures_fall_back_when_permitted(code) -> None:
    w = World(controls=fb(), deployments=three())
    provider = FakeProvider({"dep-a": [fail(code)]})
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.SUCCEEDED
    assert [c[1] for c in provider.calls] == ["dep-a", "dep-b"]
    assert [a.outcome for a in out.attempts] == ["failed", "succeeded"]
    assert w.ledger.get(out.attempts[0].reservation_id).state is ReservationState.RELEASED
    assert "model.fallback_considered" in w.telemetry.events
    assert "model.fallback_selected" in w.telemetry.events


def test_fallback_disabled_by_default_and_no_retry_storm() -> None:
    w = World(deployments=three())
    provider = FakeProvider({"dep-a": [fail("provider_unavailable")]})
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.FAILED and [c[1] for c in provider.calls] == ["dep-a"]


@pytest.mark.parametrize(
    "code",
    ["unauthenticated", "unauthorized_operation", "policy_violation", "approval_required"],
)
def test_authentication_authorization_and_policy_failures_never_fall_back(code) -> None:
    w = World(
        controls=policy(
            fallback=FallbackPolicy(enabled=True, failure_categories=tuple(FallbackFailure))
        ),
        deployments=three(),
    )
    provider = FakeProvider({"dep-a": [fail(code)], "dep-b": [ok(USAGE)]})
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.FAILED and [c[1] for c in provider.calls] == ["dep-a"]


def test_permanent_error_and_invalid_output_fall_back_only_when_configured() -> None:
    w = World(controls=fb(), deployments=three())
    provider = FakeProvider({"dep-a": [fail("model_error")]})
    assert w.invoke(provider).outcome is GovernedOutcome.FAILED
    cfg = fb(failure_categories=(FallbackFailure.PERMANENT_MODEL_ERROR,))
    w2 = World(controls=cfg, deployments=three())
    provider2 = FakeProvider({"dep-a": [fail("model_error")]})
    assert w2.invoke(provider2).outcome is GovernedOutcome.SUCCEEDED


def test_attempt_limit_and_no_loops() -> None:
    w = World(controls=fb(max_fallback_attempts=1), deployments=three())
    provider = FakeProvider(
        {n: [fail("provider_unavailable")] for n in ("dep-a", "dep-b", "dep-c")}
    )
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.FAILED and len(provider.calls) == 2  # 1 + 1 fallback
    assert len({c[1] for c in provider.calls}) == 2
    zero = World(controls=fb(max_fallback_attempts=0), deployments=three())
    p0 = FakeProvider({"dep-a": [fail("provider_unavailable")]})
    assert zero.invoke(p0).outcome is GovernedOutcome.FAILED and len(p0.calls) == 1
    one = World(controls=fb(max_fallback_attempts=3), deployments={"dep-a": {}})
    p1 = FakeProvider({"dep-a": [fail("provider_unavailable")]})
    out = one.invoke(p1)
    assert out.outcome is GovernedOutcome.FAILED and len(p1.calls) == 1  # nothing else eligible
    assert "model.fallback_rejected" in one.telemetry.events


def test_budget_and_quota_blocks_never_fall_back_and_call_no_model() -> None:
    w = World(
        controls=policy(
            budget=BudgetPolicy(initiative_limit=D("0.1")), fallback=FallbackPolicy(enabled=True)
        ),
        deployments=three(),
    )
    provider = FakeProvider()
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.BLOCKED and provider.calls == []
    q = World(
        controls=policy(
            quota=QuotaPolicy(initiative=QuotaLimits(max_invocations=0)),
            fallback=FallbackPolicy(enabled=True),
        ),
        deployments=three(),
    )
    p2 = FakeProvider()
    assert q.invoke(p2).outcome is GovernedOutcome.BLOCKED and p2.calls == []


def test_approval_required_outcome_calls_no_model() -> None:
    budget = BudgetPolicy(initiative_limit=D("0.1"), on_exhausted=BudgetAction.REQUIRE_APPROVAL)
    w = World(controls=policy(budget=budget), deployments=three())
    provider = FakeProvider()
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.APPROVAL_REQUIRED and provider.calls == []
    assert out.admission.approval_intent.operation == "model.budget_exception"


def test_timeout_is_ambiguous_not_refunded_not_retried_and_no_fallback() -> None:
    controls = policy(
        budget=BudgetPolicy(initiative_limit=D("5")),
        fallback=FallbackPolicy(enabled=True, failure_categories=tuple(FallbackFailure)),
    )
    w = World(controls=controls, deployments=three())
    provider = FakeProvider({"dep-a": [fail("remote_timeout", ExecutionStatus.TIMED_OUT)]})
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.AMBIGUOUS_OUTCOME
    assert len(provider.calls) == 1  # no duplicate execution or spending
    rid = out.attempts[0].reservation_id
    assert w.ledger.get(rid).state is ReservationState.UNRECONCILED
    assert w.snapshot().held == COST
    assert out.usage[0].status is AccountingStatus.UNRECONCILED
    assert "model.reconciliation_required" in w.telemetry.events


def test_candidate_disabled_between_planning_and_dispatch_is_reselected() -> None:
    w = World(deployments=three())

    class Racy(FallbackPlanner):
        raced = False

        def plan(self, *a, **k):
            result = super().plan(*a, **k)
            if not self.raced:
                self.raced = True
                first = result.candidates[0]
                w.reg.admin.disable(
                    ADMIN, first.deployment_id, expected_revision=first.revision, correlation_id="c"
                )
            return result

    w.planner = Racy(w.reg.reader, PROFILES)
    provider = FakeProvider()
    out = w.invoke(provider)
    assert out.outcome is GovernedOutcome.SUCCEEDED
    assert [c[1] for c in provider.calls] == ["dep-b"]  # the disabled model was never dispatched
    assert out.attempts[0].outcome == "deployment_unavailable"


def test_duplicate_invocation_is_not_double_accounted() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("5"))), deployments=three())
    provider = FakeProvider()
    first = w.invoke(provider, ref="same-call")
    again = w.invoke(provider, ref="same-call")
    assert first.outcome is again.outcome is GovernedOutcome.SUCCEEDED
    assert first.usage[0].usage_id == again.usage[0].usage_id
    assert (
        w.snapshot().spent == D("0.006000") and len(w.ledger.usage_records("travel-platform")) == 1
    )


def test_reviewer_role_flows_through_the_client_with_independence() -> None:
    w = World(
        deployments={
            "gen-a": {"model_family": "fam-a"},
            "rev-b": {"provider_id": "prov-b", "model_identifier": "m-b", "model_family": "fam-b"},
        }
    )
    generator = ModelProvenance.from_registry(
        w.reg.reader.get("gen-a"), role=ModelRole.STANDARD_REASONING, execution_ref="exec-1"
    )
    constraints = ReviewerConstraints(
        generator, ReviewRequirements(True, False, True, False, False)
    )
    provider = FakeProvider()
    out = w.invoke(
        provider, selection=base_selection(ModelRole.INDEPENDENT_REVIEWER), reviewer=constraints
    )
    assert out.outcome is GovernedOutcome.SUCCEEDED
    assert provider.calls == [("independent_reviewer", "rev-b")]
    only_gen = World(deployments={"gen-a": {"model_family": "fam-a"}})
    gen2 = ModelProvenance.from_registry(
        only_gen.reg.reader.get("gen-a"), role=ModelRole.STANDARD_REASONING, execution_ref="e"
    )
    none = only_gen.invoke(
        FakeProvider(),
        selection=base_selection(ModelRole.INDEPENDENT_REVIEWER),
        reviewer=ReviewerConstraints(gen2, ReviewRequirements(True, False, False, False, False)),
    )
    assert none.outcome is GovernedOutcome.NO_ELIGIBLE_DEPLOYMENT


# --- telemetry, configuration and boundaries --------------------------------------------------


def test_telemetry_events_metrics_and_isolation() -> None:
    w = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("5"))))
    w.invoke(FakeProvider())
    for event in ("model.budget_checked", "model.reservation_created", "model.usage_recorded"):
        assert event in w.telemetry.events
    names = {m[0] for m in w.telemetry.metrics}
    assert {
        "model.tokens.input",
        "model.tokens.output",
        "model.cost.estimated",
        "model.budget.utilization",
        "model.invocations",
    } <= names
    assert "hello" not in repr(w.telemetry.events) + repr(w.telemetry.metrics)
    failing = World(
        controls=policy(budget=BudgetPolicy(initiative_limit=D("1.0"))),
        telemetry=FakeTelemetry(fail=True),
    )
    healthy = World(controls=policy(budget=BudgetPolicy(initiative_limit=D("1.0"))))
    assert failing.admit(ref="c1").status is healthy.admit(ref="c1").status
    assert failing.admit(ref="c2").status is AdmissionStatus.BLOCKED_BUDGET  # decisions unchanged
    assert failing.invoke(FakeProvider(), ref="x").outcome is not None


def test_decisions_preserve_identifiers_and_hold_no_sensitive_data() -> None:
    w = World()
    d = w.admit()
    assert (d.initiative_id, d.request_id, d.correlation_id, d.trace_id, d.task_id) == (
        "travel-platform",
        "req-1",
        "corr-1",
        "trace-1",
        "task-1",
    )
    assert d.registry_revision and d.budget_revision == 1
    assert set(type(d).model_fields).isdisjoint(
        {"prompt", "authorization", "credentials", "principal"}
    )


def test_existing_profiles_load_with_safe_defaults() -> None:
    controls = ModelGovernancePolicy()
    assert (
        controls.budget.initiative_limit is None
        and controls.budget.on_exhausted is BudgetAction.BLOCK
    )
    assert controls.fallback.enabled is False and controls.fallback.max_fallback_attempts == 2
    assert controls.quota.initiative.max_invocations is None
    assert RoutingEnv().profile.inference_controls == controls
    for bad in (
        lambda: BudgetPolicy(
            role_limits=(RoleAmount(role=ProfileRole.ROUTING, amount=D("1")),) * 2
        ),
        lambda: QuotaPolicy(
            role_limits=(RoleQuota(role=ProfileRole.ROUTING, limits=QuotaLimits()),) * 2
        ),
        lambda: FallbackPolicy(failure_categories=(FallbackFailure.TRANSIENT,) * 2),
        lambda: FallbackPolicy(max_fallback_attempts=9),
        lambda: FallbackPolicy(priorities=(DeploymentPriority(deployment_id="a", priority=1),) * 2),
        lambda: QuotaLimits(max_tokens=-1),
    ):
        with pytest.raises(ValidationError):
            bad()


def test_no_vendor_pricing_no_policy_side_model_calls_and_sdk_untouched() -> None:
    package = Path(gov_pkg.__file__).parent
    text = "".join(p.read_text().lower() for p in package.glob("*.py"))
    for banned in ("claude", "gpt-", "bedrock", "anthropic", "titan", "llama", "arn:", "api_key"):
        assert banned not in text
    callers = []
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        if any(isinstance(n, ast.Attribute) and n.attr == "invoke_model" for n in ast.walk(tree)):
            callers.append(path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("ai_dlc.adapters", "ai_dlc.application.gateway"))
    assert callers == ["client.py"]  # policy/accounting/fallback logic never invokes a model
    harness = package.parent / "agent_harness"
    assert all("model_governance" not in p.read_text() for p in harness.glob("*.py"))
    assert not any(word in text for word in ("a2aclient", "delegate"))
