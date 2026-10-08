"""Offline tests for reasoning-tier escalation policy (AIDLC-50)."""

from __future__ import annotations

import ast
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_deterministic_routing import Env, FakeTelemetry

import ai_dlc.application.reasoning as reasoning_pkg
from ai_dlc.application.agent_harness import (
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    ModelRole,
    ReasoningLevel,
)
from ai_dlc.application.reasoning import (
    DecisionKind,
    ExecutionObservation,
    ReasoningDecision,
    ReasoningFailure,
    ReasoningSignals,
    ReasoningTierPolicy,
    RejectionReason,
    TriggerCode,
    classify_failure,
    evaluate_triggers,
)
from ai_dlc.domain.identity import Capability
from ai_dlc.domain.initiative import load_initiative_profile
from ai_dlc.domain.initiative.models import ReasoningPolicy

STD, DEEP = ModelRole.STANDARD_REASONING, ModelRole.DEEP_REASONING
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
FAIL = ReasoningFailure.INSUFFICIENT_REASONING


def setup(telemetry=None, **policy):
    env = Env()
    profile = env.profile.model_copy(update={"reasoning": ReasoningPolicy(**policy)})
    engine = ReasoningTierPolicy(telemetry or env.telemetry, clock=lambda: NOW)
    return env, profile, engine


def decide(signals=None, *, revision=4, env_policy=None, **policy):
    env, profile, engine = setup(**policy)
    signals = signals or ReasoningSignals()
    return engine.evaluate(
        signals, context=env.context, profile=profile, initiative_revision=revision
    )


def result(code: str, status=ExecutionStatus.FAILED) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        correlation_id="c",
        trace_id="t",
        error=ExecutionError(code=code, message="provider error"),
    )


# --- selection --------------------------------------------------------------------------------


def test_default_is_standard() -> None:
    d = decide()
    assert (d.kind, d.selected_role, d.escalated) == (DecisionKind.STANDARD_SELECTED, STD, False)
    assert d.trigger_codes == () and d.previous_role is None and d.escalation_count == 0


@pytest.mark.parametrize(
    ("signals", "code"),
    [
        (ReasoningSignals(estimated_context_tokens=64_000), TriggerCode.CONTEXT_SIZE_THRESHOLD),
        (ReasoningSignals(complexity_score=0.7), TriggerCode.TASK_COMPLEXITY_THRESHOLD),
        (ReasoningSignals(affected_components=10), TriggerCode.TASK_BREADTH_THRESHOLD),
        (ReasoningSignals(repositories=3), TriggerCode.TASK_BREADTH_THRESHOLD),
        (ReasoningSignals(dependency_relationships=25), TriggerCode.TASK_BREADTH_THRESHOLD),
        (ReasoningSignals(failures=(FAIL, FAIL)), TriggerCode.FAILED_ATTEMPT_THRESHOLD),
        (
            ReasoningSignals(required_reasoning_level=ReasoningLevel.EXTENDED),
            TriggerCode.CAPABILITY_REQUIREMENT,
        ),
        (ReasoningSignals(requested_role=DEEP), TriggerCode.EXPLICIT_REQUEST),
    ],
)
def test_each_trigger_selects_deep_initially(signals, code) -> None:
    d = decide(signals)
    assert d.trigger_codes == (code,)
    assert (d.kind, d.selected_role, d.escalated) == (DecisionKind.DEEP_SELECTED, DEEP, False)
    assert d.escalation_count == 0  # an initial deep selection is not an escalation


@pytest.mark.parametrize(
    "signals",
    [
        ReasoningSignals(estimated_context_tokens=63_999),
        ReasoningSignals(complexity_score=0.69),
        ReasoningSignals(affected_components=9, repositories=2, dependency_relationships=24),
        ReasoningSignals(failures=(FAIL,)),
        ReasoningSignals(required_reasoning_level=ReasoningLevel.MODERATE),
    ],
)
def test_just_below_thresholds_stay_standard(signals) -> None:
    assert decide(signals).selected_role is STD


def test_capability_requirement_from_policy_capabilities() -> None:
    d = decide(
        ReasoningSignals(required_capabilities=frozenset({Capability.IMPLEMENTATION})),
        deep_capabilities=(Capability.IMPLEMENTATION,),
    )
    assert d.trigger_codes == (TriggerCode.CAPABILITY_REQUIREMENT,)
    assert (
        decide(
            ReasoningSignals(required_capabilities=frozenset({Capability.IMPLEMENTATION}))
        ).selected_role
        is STD
    )


def test_multiple_triggers_are_ordered_and_deduplicated() -> None:
    signals = ReasoningSignals(
        estimated_context_tokens=10**6,
        complexity_score=0.9,
        affected_components=50,
        repositories=9,
        failures=(FAIL, FAIL, FAIL),
        required_reasoning_level=ReasoningLevel.EXTENDED,
        requested_role=DEEP,
    )
    d = decide(signals, always_deep=True)
    assert d.trigger_codes == tuple(TriggerCode)  # fixed enum order, breadth appears once
    again = decide(signals, always_deep=True)
    assert again.trigger_codes == d.trigger_codes


def test_thresholds_can_be_disabled_or_changed_per_initiative() -> None:
    big = ReasoningSignals(estimated_context_tokens=500_000, complexity_score=1.0)
    assert (
        decide(
            big, context_size_threshold_tokens=None, complexity_score_threshold=None
        ).selected_role
        is STD
    )
    assert (
        decide(
            ReasoningSignals(estimated_context_tokens=10), context_size_threshold_tokens=5
        ).selected_role
        is DEEP
    )


def test_always_deep_governed_policy() -> None:
    d = decide(always_deep=True)
    assert d.selected_role is DEEP and d.trigger_codes == (TriggerCode.EXPLICIT_GOVERNED_POLICY,)


# --- escalation -------------------------------------------------------------------------------


def test_standard_to_deep_escalation_is_distinct_from_initial_selection() -> None:
    d = decide(ReasoningSignals(prior_role=STD, failures=(FAIL, FAIL)))
    assert (d.kind, d.selected_role, d.previous_role, d.escalated) == (
        DecisionKind.ESCALATED,
        DEEP,
        STD,
        True,
    )
    assert d.escalation_count == 1 and d.trigger_codes == (TriggerCode.FAILED_ATTEMPT_THRESHOLD,)


def test_standard_retained_without_triggers() -> None:
    d = decide(ReasoningSignals(prior_role=STD))
    assert d.kind is DecisionKind.RETAINED and d.selected_role is STD and d.escalation_count == 0


def test_escalation_disabled_rejects_but_initial_selection_still_works() -> None:
    d = decide(ReasoningSignals(prior_role=STD, failures=(FAIL, FAIL)), escalation_enabled=False)
    assert d.kind is DecisionKind.ESCALATION_REJECTED and d.selected_role is STD
    assert d.rejection_reason is RejectionReason.ESCALATION_DISABLED
    assert (
        decide(ReasoningSignals(complexity_score=1.0), escalation_enabled=False).selected_role
        is DEEP
    )


def test_deep_reasoning_prohibited() -> None:
    initial = decide(ReasoningSignals(complexity_score=1.0), deep_reasoning_allowed=False)
    assert (
        initial.selected_role is STD
        and initial.rejection_reason is RejectionReason.DEEP_NOT_ALLOWED
    )
    assert initial.trigger_codes  # the triggers are still recorded
    esc = decide(
        ReasoningSignals(prior_role=STD, complexity_score=1.0), deep_reasoning_allowed=False
    )
    assert esc.kind is DecisionKind.ESCALATION_REJECTED and esc.selected_role is STD
    assert esc.rejection_reason is RejectionReason.DEEP_NOT_ALLOWED
    always = decide(deep_reasoning_allowed=False, always_deep=True)
    assert always.selected_role is STD


def test_escalation_limit_reached_and_zero_limit() -> None:
    d = decide(ReasoningSignals(prior_role=STD, complexity_score=1.0), max_escalations_per_task=0)
    assert d.kind is DecisionKind.ESCALATION_REJECTED
    assert d.rejection_reason is RejectionReason.ESCALATION_LIMIT_REACHED


def test_already_deep_never_escalates_further_or_downgrades() -> None:
    for count in (0, 1):
        d = decide(
            ReasoningSignals(
                prior_role=DEEP, escalation_count=count, failures=(FAIL,) * 5, complexity_score=1.0
            )
        )
        assert d.kind is DecisionKind.RETAINED and d.selected_role is DEEP
        assert d.escalation_count == count and not d.escalated
    quiet = decide(ReasoningSignals(prior_role=DEEP, escalation_count=1))
    assert quiet.selected_role is DEEP  # no triggers still means no automatic downgrade


def test_repeated_evaluation_cannot_reset_or_inflate_count() -> None:
    first = decide(ReasoningSignals(prior_role=STD, complexity_score=1.0))
    assert first.escalation_count == 1
    for _ in range(5):
        again = decide(
            ReasoningSignals(
                prior_role=first.selected_role,
                escalation_count=first.escalation_count,
                complexity_score=1.0,
            )
        )
        assert again.escalation_count == 1 and again.selected_role is DEEP
    # history fed back as standard with a count is rejected rather than reset
    forged = decide(ReasoningSignals(prior_role=STD, escalation_count=1, complexity_score=1.0))
    assert (
        forged.kind is DecisionKind.BLOCKED
        and forged.rejection_reason is RejectionReason.INVALID_HISTORY
    )
    unknown_prior = decide(ReasoningSignals(prior_role=None, escalation_count=2))
    assert unknown_prior.kind is DecisionKind.BLOCKED


# --- failure classification -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("provider_unavailable", ReasoningFailure.TRANSIENT),
        ("rate_limited", ReasoningFailure.THROTTLED),
        ("model_timeout", ReasoningFailure.TIMEOUT),
        ("malformed_json", ReasoningFailure.INVALID_OUTPUT),
        ("permission_denied", ReasoningFailure.AUTHORIZATION),
        ("authentication_failed", ReasoningFailure.AUTHENTICATION),
        ("context_length_exceeded", ReasoningFailure.CONTEXT_LIMITATION),
        ("approval_required", ReasoningFailure.POLICY_VALIDATION),
        ("something_new", ReasoningFailure.UNKNOWN),
    ],
)
def test_failure_classification_reuses_harness_taxonomy(code, expected) -> None:
    assert classify_failure(result(code)) is expected


def test_classification_never_yields_insufficient_reasoning_or_trusts_model_claims() -> None:
    for code in ("needs_deeper_reasoning", "escalate_to_deep", "insufficient_reasoning"):
        assert classify_failure(result(code)) is ReasoningFailure.UNKNOWN
    with pytest.raises(ValueError):
        classify_failure(
            ExecutionResult(status=ExecutionStatus.SUCCEEDED, correlation_id="c", trace_id="t")
        )


@pytest.mark.parametrize(
    "kind",
    [
        ReasoningFailure.TRANSIENT,
        ReasoningFailure.THROTTLED,
        ReasoningFailure.TIMEOUT,
        ReasoningFailure.INVALID_OUTPUT,
        ReasoningFailure.CONTEXT_LIMITATION,
        ReasoningFailure.UNKNOWN,
        ReasoningFailure.CANCELLED,
    ],
)
def test_non_reasoning_failures_do_not_escalate(kind) -> None:
    d = decide(ReasoningSignals(prior_role=STD, failures=(kind,) * 10))
    assert d.kind is DecisionKind.RETAINED and d.trigger_codes == ()


@pytest.mark.parametrize(
    "kind",
    sorted(ReasoningFailure.__members__.values())[:0]
    or [
        ReasoningFailure.AUTHORIZATION,
        ReasoningFailure.AUTHENTICATION,
        ReasoningFailure.POLICY_VALIDATION,
    ],
)
def test_auth_and_policy_failures_fail_closed(kind) -> None:
    d = decide(ReasoningSignals(prior_role=STD, failures=(FAIL, FAIL, kind), complexity_score=1.0))
    assert d.kind is DecisionKind.BLOCKED and d.selected_role is None
    assert d.rejection_reason is RejectionReason.FAILURE_NOT_ESCALATABLE and not d.escalated


# --- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"estimated_context_tokens": -1},
        {"estimated_context_tokens": 10**12},
        {"estimated_context_tokens": 1.5},
        {"estimated_context_tokens": True},
        {"affected_components": -1},
        {"repositories": -3},
        {"dependency_relationships": -1},
        {"complexity_score": math.nan},
        {"complexity_score": math.inf},
        {"complexity_score": -0.1},
        {"complexity_score": 1.1},
        {"complexity_score": True},
        {"complexity_score": "high"},
        {"required_reasoning_level": 9},
        {"required_reasoning_level": "genius"},
        {"required_capabilities": frozenset({"telepathy"})},
        {"failures": ("exploded",)},
        {"failures": (FAIL,) * 51},
        {"prior_role": ModelRole.ROUTING},
        {"requested_role": ModelRole.INDEPENDENT_REVIEWER},
        {"escalation_count": -1},
        {"escalation_count": 11},
        {"escalation_count": True},
    ],
)
def test_invalid_signals_are_rejected(kwargs) -> None:
    with pytest.raises((ValueError, TypeError)):
        ReasoningSignals(**kwargs)


def test_policy_validation() -> None:
    for bad in (
        {"max_escalations_per_task": -1},
        {"max_escalations_per_task": 9},
        {"complexity_score_threshold": math.nan},
        {"complexity_score_threshold": 2},
        {"context_size_threshold_tokens": 0},
        {"failed_attempt_threshold": 0},
        {"deep_reasoning_level": 7},
        {"unknown_setting": 1},
        {"deep_capabilities": (Capability.CODE_ANALYSIS, Capability.CODE_ANALYSIS)},
    ):
        with pytest.raises(ValueError):
            ReasoningPolicy(**bad)


# --- initiatives and context ------------------------------------------------------------------


def test_cross_initiative_policy_mismatch_fails_closed() -> None:
    env, profile, engine = setup()
    other = load_initiative_profile(
        Path(reasoning_pkg.__file__).parents[4]
        / "configs"
        / "initiatives"
        / "examples"
        / "field-operations.yaml"
    ).model_copy(update={"reasoning": ReasoningPolicy(always_deep=True)})
    d = engine.evaluate(
        ReasoningSignals(), context=env.context, profile=other, initiative_revision=2
    )
    assert d.kind is DecisionKind.BLOCKED
    assert d.rejection_reason is RejectionReason.POLICY_INITIATIVE_MISMATCH
    assert d.initiative_id == "travel-platform"


def test_policies_differ_per_initiative_and_defaults_load() -> None:
    legacy = load_initiative_profile(
        Path(reasoning_pkg.__file__).parents[4]
        / "configs"
        / "initiatives"
        / "examples"
        / "travel-platform.yaml"
    )
    assert legacy.reasoning == ReasoningPolicy()
    strict = decide(
        ReasoningSignals(estimated_context_tokens=1000), context_size_threshold_tokens=500
    )
    lax = decide(
        ReasoningSignals(estimated_context_tokens=1000), context_size_threshold_tokens=5000
    )
    assert (strict.selected_role, lax.selected_role) == (DEEP, STD)


def test_identifiers_revision_and_context_preserved() -> None:
    env, profile, engine = setup()
    before = repr(env.context)
    d = engine.evaluate(
        ReasoningSignals(complexity_score=1.0),
        context=env.context,
        profile=profile,
        initiative_revision=7,
    )
    assert (
        d.policy_revision,
        d.initiative_id,
        d.request_id,
        d.correlation_id,
        d.trace_id,
        d.task_id,
    ) == (7, "travel-platform", "req-1", "corr-1", "trace-1", "task-1")
    assert d.decision_timestamp == NOW and repr(env.context) == before
    with pytest.raises(ValueError):
        engine.evaluate(
            ReasoningSignals(), context=env.context, profile=profile, initiative_revision=0
        )
    with pytest.raises(TypeError):
        engine.evaluate(
            {"complexity_score": 1}, context=env.context, profile=profile, initiative_revision=1
        )  # type: ignore[arg-type]


def test_user_text_cannot_influence_policy() -> None:
    fields = set(ReasoningSignals.__slots__)
    assert not fields & {"content", "text", "prompt", "message", "request"}
    assert not {"content", "prompt"} & set(ReasoningDecision.__slots__)


def test_evaluate_triggers_is_deterministic() -> None:
    signals = ReasoningSignals(complexity_score=0.9, repositories=5)
    assert evaluate_triggers(signals, ReasoningPolicy()) == evaluate_triggers(
        signals, ReasoningPolicy()
    )


# --- records ----------------------------------------------------------------------------------


def test_decision_record_is_serializable_and_round_trips() -> None:
    d = decide(ReasoningSignals(prior_role=STD, failures=(FAIL, FAIL), requested_role=STD))
    data = d.to_dict()
    text = json.dumps(data)
    assert ReasoningDecision.from_dict(json.loads(text)) == d
    assert set(data) >= {
        "selected_role",
        "previous_role",
        "trigger_codes",
        "decision_timestamp",
        "task_id",
        "correlation_id",
        "policy_revision",
        "escalation_count",
    }
    assert not any(w in text.lower() for w in ("claude", "gpt", "bedrock", "arn:", "prompt"))


def test_requested_selected_and_invoked_tiers_are_distinct() -> None:
    d = decide(ReasoningSignals(requested_role=DEEP), deep_reasoning_allowed=False)
    assert d.requested_role is DEEP and d.selected_role is STD
    assert d.execution_status is ExecutionObservation.NOT_OBSERVED and d.invoked_role is None
    invoked = d.record_invocation(STD)
    assert invoked.execution_status is ExecutionObservation.INVOKED and invoked.invoked_role is STD
    assert d.invoked_role is None  # the original record is immutable
    assert ReasoningDecision.from_dict(invoked.to_dict()) == invoked
    with pytest.raises(ValueError):
        d.record_invocation(ModelRole.ROUTING)
    with pytest.raises(ValueError):
        ReasoningDecision.from_dict({**d.to_dict(), "invoked_role": "standard_reasoning"})


# --- telemetry --------------------------------------------------------------------------------


def test_telemetry_events_and_metrics() -> None:
    t = FakeTelemetry()
    env, profile, engine = setup(telemetry=t, max_escalations_per_task=0)

    def go(signals):
        engine.evaluate(signals, context=env.context, profile=profile, initiative_revision=1)

    go(ReasoningSignals())
    go(ReasoningSignals(complexity_score=1.0))
    go(ReasoningSignals(prior_role=STD, complexity_score=1.0))
    assert t.events == [
        "reasoning.standard_selected",
        "reasoning.deep_selected",
        "reasoning.escalation_requested",
        "reasoning.escalation_rejected",
        "reasoning.escalation_limit_reached",
    ]
    names = [m[0] for m in t.metrics]
    assert "reasoning.decisions.standard" in names and "reasoning.decisions.deep" in names
    assert "reasoning.escalation_rejections" in names
    assert "reasoning.trigger.task_complexity_threshold" in names
    t2 = FakeTelemetry()
    env2, profile2, engine2 = setup(telemetry=t2)
    engine2.evaluate(
        ReasoningSignals(prior_role=STD, complexity_score=1.0),
        context=env2.context,
        profile=profile2,
        initiative_revision=1,
    )
    assert t2.events == [
        "reasoning.escalation_requested",
        "reasoning.escalation_approved",
        "reasoning.deep_selected",
    ]
    assert ("reasoning.escalations", 1.0) in t2.metrics


def test_telemetry_failure_does_not_change_decision() -> None:
    signals = ReasoningSignals(prior_role=STD, complexity_score=1.0)
    ok = decide(signals)
    env, profile, engine = setup(telemetry=FakeTelemetry(fail=True))
    got = engine.evaluate(signals, context=env.context, profile=profile, initiative_revision=4)
    assert got == ok
    quiet = ReasoningTierPolicy(clock=lambda: NOW)
    assert (
        quiet.evaluate(signals, context=env.context, profile=profile, initiative_revision=4) == ok
    )


# --- boundaries -------------------------------------------------------------------------------


def test_no_model_provider_a2a_registry_or_vendor_names() -> None:
    package = Path(reasoning_pkg.__file__).parent
    forbidden = (
        "ModelProvider",
        "invoke_model",
        "A2AClient",
        "AgentDelegator",
        "delegate",
        "model_registry",
        "ModelRegistry",
        "boto3",
        "claude",
        "gpt",
        "bedrock",
        "anthropic",
        "arn:",
        "ToolProvider",
    )
    for path in package.glob("*.py"):
        text = path.read_text()
        for word in forbidden:
            assert word.lower() not in text.lower(), (path.name, word)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("ai_dlc.adapters", "ai_dlc.application.gateway"))


def test_sdk_does_not_import_reasoning_and_existing_contracts_unchanged() -> None:
    from ai_dlc.application.agent_harness import INTERFACE_VERSION

    harness = Path(reasoning_pkg.__file__).parents[1] / "agent_harness"
    assert all("application.reasoning" not in p.read_text() for p in harness.glob("*.py"))
    assert INTERFACE_VERSION == "1.5.0"
    assert len(ModelRole) == 4
