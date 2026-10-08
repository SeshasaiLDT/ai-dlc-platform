"""Offline tests for deterministic capability routing (AIDLC-48)."""

from __future__ import annotations

import ast
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import ai_dlc.application.routing as routing_pkg
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.application.agent_harness import AgentContext, create_agent_context
from ai_dlc.application.authorization import (
    AuthorizationAuditError,
    AuthorizationService,
    RoleGrant,
    RolePolicy,
    resolve_authorization_context,
)
from ai_dlc.application.routing import (
    CLASSIFIER_ELIGIBLE_REASONS,
    DeterministicRouter,
    RoutingDecision,
    RoutingOutcome,
    RoutingReason,
    RoutingRequest,
    RoutingRule,
)
from ai_dlc.domain.identity import Capability, InitiativeMembership, Principal, Role
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile
from ai_dlc.domain.initiative.models import CapabilityRoute, Routing, WorkflowRoute

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
NOW = datetime(2026, 10, 8, tzinfo=UTC)
PRINCIPAL = Principal("user-1", "test")
INV, IMPACT, CODE, IMPL = (
    Capability.INVESTIGATION,
    Capability.CHANGE_IMPACT,
    Capability.CODE_ANALYSIS,
    Capability.IMPLEMENTATION,
)

POLICY = RolePolicy(
    (
        RoleGrant(Role.ANALYST, capabilities=frozenset({INV, IMPACT, CODE})),
        RoleGrant(Role.DEVELOPER, capabilities=frozenset({IMPL})),
    )
)


def routing(**kwargs) -> Routing:
    caps = kwargs.pop("caps", (INV, IMPACT, CODE, IMPL))
    return Routing(
        capabilities=tuple(CapabilityRoute(capability=c) for c in caps),
        workflows=kwargs.pop(
            "workflows",
            (
                WorkflowRoute(id="code-impact", capability=CODE),
                WorkflowRoute(id="flow-impact", capability=IMPACT),
                WorkflowRoute(id="code-generation", capability=IMPL),
            ),
        ),
    )


def profile(initiative: str = "travel-platform", **kwargs) -> InitiativeProfile:
    base = load_initiative_profile(EXAMPLES / f"{initiative}.yaml")
    return base.model_copy(update={"routing": routing(**kwargs)})


class FakeTelemetry:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[str] = []
        self.metrics: list[tuple[str, float]] = []
        self.fail = fail

    def record_event(self, name: str, *, context: AgentContext) -> None:
        if self.fail:
            raise RuntimeError("telemetry down")
        self.events.append(name)

    def record_metric(self, name: str, value: float, *, context: AgentContext) -> None:
        if self.fail:
            raise RuntimeError("telemetry down")
        self.metrics.append((name, value))

    def record_error(self, error, *, context) -> None:
        raise AssertionError("not used")


class Env:
    def __init__(self, roles=(Role.ANALYST,), initiative="travel-platform", telemetry=None, **kw):
        self.profile = profile(initiative, **kw)
        self.sink = InMemoryAuthorizationAuditSink()
        memberships = InMemoryMembershipRepository(
            (InitiativeMembership(PRINCIPAL.subject_id, initiative, roles),)
        )
        self.auth = AuthorizationService(memberships, POLICY, self.sink, clock=lambda: NOW)
        resolved = resolve_authorization_context(
            PRINCIPAL, initiative, self.profile, memberships, POLICY
        )
        self.context = create_agent_context(
            task_id="task-1",
            authorization=resolved,
            request_id="req-1",
            correlation_id="corr-1",
            trace_id="trace-1",
        )
        self.telemetry = telemetry or FakeTelemetry()
        self.router = DeterministicRouter(self.auth, self.telemetry)

    def route(self, request=None, *, revision=3, profile_=None, **kwargs) -> RoutingDecision:
        request = request or RoutingRequest(**kwargs)
        return self.router.route(
            request,
            context=self.context,
            profile=profile_ or self.profile,
            initiative_revision=revision,
        )


# --- explicit selection ----------------------------------------------------------------------


def test_explicit_capability_routes_when_authorized_and_configured() -> None:
    d = Env().route(explicit_capability="investigation")
    assert (d.outcome, d.rule, d.reason) == (
        RoutingOutcome.ROUTED,
        RoutingRule.EXPLICIT_CAPABILITY,
        RoutingReason.EXPLICIT_CAPABILITY_SELECTED,
    )
    assert d.capability is INV and not d.classifier_candidate


def test_unauthorized_explicit_selection_is_denied_and_never_a_candidate() -> None:
    env = Env()
    d = env.route(explicit_capability="implementation")  # analyst lacks it
    assert d.outcome is RoutingOutcome.DENIED and d.capability is None
    assert d.reason is RoutingReason.CAPABILITY_NOT_AUTHORIZED
    assert not d.classifier_candidate
    assert env.telemetry.events == ["routing.denied"]


def test_explicit_denial_does_not_fall_through_to_workflow() -> None:
    d = Env().route(explicit_capability="implementation", workflow_ids=("code-impact",))
    assert d.outcome is RoutingOutcome.DENIED and d.capability is None


def test_unknown_capability_is_invalid_not_unresolved() -> None:
    d = Env().route(explicit_capability="teleportation")
    assert d.outcome is RoutingOutcome.INVALID_REQUEST
    assert d.reason is RoutingReason.UNKNOWN_CAPABILITY and not d.classifier_candidate


def test_disabled_and_unconfigured_capability_are_configuration_errors() -> None:
    env = Env()
    disabled = env.profile.model_copy(
        update={
            "routing": Routing(
                capabilities=(CapabilityRoute(capability=INV, enabled=False),),
            )
        }
    )
    d = env.route(explicit_capability="investigation", profile_=disabled)
    assert (d.outcome, d.reason) == (
        RoutingOutcome.CONFIGURATION_ERROR,
        RoutingReason.CAPABILITY_DISABLED,
    )
    missing = Env(caps=(CODE,))
    d = missing.route(explicit_capability="investigation")
    assert d.reason is RoutingReason.CAPABILITY_NOT_CONFIGURED
    assert (
        not d.classifier_candidate
        and not missing.route(explicit_capability="investigation").capability
    )


def test_unconfigured_capability_does_not_leak_to_unauthorized_user() -> None:
    d = Env(caps=()).route(explicit_capability="implementation")
    assert d.outcome is RoutingOutcome.DENIED  # authorization is checked before configuration


# --- workflows --------------------------------------------------------------------------------


def test_known_workflow_routes_to_its_capability() -> None:
    env = Env()
    d = env.route(workflow_ids=("code-impact",))
    assert (d.outcome, d.rule, d.reason) == (
        RoutingOutcome.ROUTED,
        RoutingRule.CONFIGURED_WORKFLOW,
        RoutingReason.WORKFLOW_MATCHED,
    )
    assert d.capability is CODE and d.workflow_id == "code-impact"
    assert env.telemetry.events == ["routing.workflow_matched"]


def test_workflow_capability_is_authorized() -> None:
    d = Env().route(workflow_ids=("code-generation",))  # maps to implementation; analyst denied
    assert d.outcome is RoutingOutcome.DENIED and d.capability is None
    assert Env(roles=(Role.DEVELOPER,)).route(workflow_ids=("code-generation",)).capability is IMPL


def test_disabled_workflow_does_not_route_and_is_not_a_candidate() -> None:
    d = Env(workflows=(WorkflowRoute(id="code-impact", capability=CODE, enabled=False),)).route(
        workflow_ids=("code-impact",)
    )
    assert d.outcome is RoutingOutcome.UNRESOLVED and d.reason is RoutingReason.WORKFLOW_DISABLED
    assert d.capability is None and not d.classifier_candidate


def test_unknown_workflow_is_unresolved_candidate_and_not_echoed() -> None:
    d = Env().route(workflow_ids=("something-else",))
    assert d.outcome is RoutingOutcome.UNRESOLVED and d.reason is RoutingReason.UNKNOWN_WORKFLOW
    assert d.classifier_candidate and d.workflow_id is None


def test_workflow_from_other_initiative_does_not_leak() -> None:
    other = profile(
        "field-operations", workflows=(WorkflowRoute(id="secret-flow", capability=INV),)
    )
    assert other.initiative.id != "travel-platform"
    d = Env().route(workflow_ids=("secret-flow",))
    assert d.reason is RoutingReason.UNKNOWN_WORKFLOW
    # supplying another initiative's profile with this context is denied, not routed
    mismatched = Env().route(workflow_ids=("secret-flow",), profile_=other)
    assert mismatched.outcome is RoutingOutcome.DENIED and mismatched.capability is None


def test_duplicate_workflow_mappings_fail_closed() -> None:
    with pytest.raises(ValidationError):
        routing(
            workflows=(
                WorkflowRoute(id="dup", capability=INV),
                WorkflowRoute(id="dup", capability=IMPACT),
            )
        )
    env = Env()
    bad = Routing.model_construct(
        capabilities=env.profile.routing.capabilities,
        workflows=(
            WorkflowRoute(id="dup", capability=INV),
            WorkflowRoute(id="dup", capability=IMPACT),
        ),
    )
    d = env.route(workflow_ids=("dup",), profile_=env.profile.model_copy(update={"routing": bad}))
    assert d.outcome is RoutingOutcome.CONFIGURATION_ERROR
    assert d.reason is RoutingReason.DUPLICATE_WORKFLOW_MAPPING and not d.classifier_candidate
    with pytest.raises(ValidationError):
        Routing(capabilities=(CapabilityRoute(capability=INV), CapabilityRoute(capability=INV)))


def test_workflow_pointing_at_unavailable_capability_is_configuration_error() -> None:
    d = Env(caps=(INV,)).route(workflow_ids=("code-impact",))
    assert d.outcome is RoutingOutcome.CONFIGURATION_ERROR
    assert d.reason is RoutingReason.WORKFLOW_CAPABILITY_UNAVAILABLE and not d.classifier_candidate


def test_ambiguous_workflows_are_unresolved_candidates() -> None:
    env = Env()
    d = env.route(workflow_ids=("code-impact", "flow-impact"))
    assert d.outcome is RoutingOutcome.UNRESOLVED and d.reason is RoutingReason.AMBIGUOUS_WORKFLOW
    assert d.classifier_candidate and d.capability is None
    same = Env(
        workflows=(
            WorkflowRoute(id="wf-a", capability=CODE),
            WorkflowRoute(id="wf-b", capability=CODE),
        )
    )
    assert same.route(workflow_ids=("wf-a", "wf-b")).capability is CODE


# --- precedence -------------------------------------------------------------------------------


def test_explicit_selection_takes_precedence_over_unlocked_workflow() -> None:
    env = Env(
        workflows=(WorkflowRoute(id="code-impact", capability=CODE, capability_locked=False),)
    )
    d = env.route(explicit_capability="investigation", workflow_ids=("code-impact",))
    assert d.capability is INV and d.rule is RoutingRule.EXPLICIT_CAPABILITY


def test_locked_workflow_conflicting_with_explicit_selection_fails_closed() -> None:
    d = Env().route(explicit_capability="investigation", workflow_ids=("code-impact",))
    assert d.outcome is RoutingOutcome.INVALID_REQUEST
    assert d.reason is RoutingReason.WORKFLOW_CAPABILITY_CONFLICT and d.capability is None
    agree = Env().route(explicit_capability="code_analysis", workflow_ids=("code-impact",))
    assert agree.capability is CODE


def test_disabled_workflow_does_not_conflict_with_explicit() -> None:
    env = Env(workflows=(WorkflowRoute(id="code-impact", capability=CODE, enabled=False),))
    assert (
        env.route(explicit_capability="investigation", workflow_ids=("code-impact",)).capability
        is INV
    )


# --- invalid / unresolved ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"explicit_capability": "bad id!"},
        {"explicit_capability": ""},
        {"workflow_ids": ("a", "a")},
        {"workflow_ids": tuple(f"w{i}" for i in range(20))},
        {"workflow_ids": ("has space",)},
        {"content": "x" * 200_000},
    ],
)
def test_malformed_requests_are_invalid(kwargs) -> None:
    d = Env().route(**kwargs)
    assert d.outcome is RoutingOutcome.INVALID_REQUEST and not d.classifier_candidate


def test_empty_and_wrongly_typed_requests() -> None:
    assert Env().route().reason is RoutingReason.EMPTY_REQUEST
    assert Env().route(content="   ").reason is RoutingReason.EMPTY_REQUEST
    with pytest.raises(TypeError):
        RoutingRequest(workflow_ids=["a"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Env().router.route(
            {"explicit_capability": "x"},
            context=Env().context,
            profile=Env().profile,
            initiative_revision=1,
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Env().route(RoutingRequest(content="x"), revision=0)


def test_free_text_only_is_unresolved_candidate_without_substring_matching() -> None:
    env = Env()
    d = env.route(content="please run code-impact and investigation on code_analysis now")
    assert d.outcome is RoutingOutcome.UNRESOLVED
    assert d.reason is RoutingReason.NO_DETERMINISTIC_MATCH
    assert d.classifier_candidate and d.capability is None and d.rule is RoutingRule.NONE


def test_classifier_candidate_only_for_expected_reasons() -> None:
    assert CLASSIFIER_ELIGIBLE_REASONS == {
        RoutingReason.UNKNOWN_WORKFLOW,
        RoutingReason.AMBIGUOUS_WORKFLOW,
        RoutingReason.NO_DETERMINISTIC_MATCH,
    }
    for outcome in (
        RoutingOutcome.DENIED,
        RoutingOutcome.INVALID_REQUEST,
        RoutingOutcome.CONFIGURATION_ERROR,
    ):
        d = RoutingDecision(
            outcome, RoutingRule.NONE, RoutingReason.NO_DETERMINISTIC_MATCH, "i", 1, "r", "c", "t"
        )
        assert not d.classifier_candidate
    with pytest.raises(ValueError):
        RoutingDecision(
            RoutingOutcome.UNRESOLVED,
            RoutingRule.NONE,
            RoutingReason.CAPABILITY_DISABLED,
            "i",
            1,
            "r",
            "c",
            "t",
        )
    with pytest.raises(ValueError):
        RoutingDecision(
            RoutingOutcome.ROUTED,
            RoutingRule.NONE,
            RoutingReason.WORKFLOW_MATCHED,
            "i",
            1,
            "r",
            "c",
            "t",
            capability=INV,
        )


# --- context, revision, determinism, telemetry -----------------------------------------------


def test_identifiers_and_revision_preserved_and_context_unchanged() -> None:
    env = Env()
    before = repr(env.context)
    d = env.route(explicit_capability="investigation", revision=7)
    assert (d.request_id, d.correlation_id, d.trace_id, d.task_id) == (
        "req-1",
        "corr-1",
        "trace-1",
        "task-1",
    )
    assert d.initiative_id == "travel-platform" and d.initiative_revision == 7
    assert repr(env.context) == before


def test_new_profile_revision_changes_outcome_deterministically() -> None:
    env = Env()
    v1 = env.route(workflow_ids=("code-impact",), revision=1)
    off = env.profile.model_copy(
        update={
            "routing": routing(
                workflows=(WorkflowRoute(id="code-impact", capability=CODE, enabled=False),)
            )
        }
    )
    v2 = env.route(workflow_ids=("code-impact",), revision=2, profile_=off)
    assert v1.capability is CODE and v2.capability is None and v2.initiative_revision == 2
    assert env.route(workflow_ids=("code-impact",), revision=1) == v1


def test_authorization_is_via_existing_service_and_audited() -> None:
    env = Env()
    env.route(explicit_capability="investigation", revision=5)
    (event,) = env.sink.events
    assert event.principal_id == "user-1" and event.allowed
    env.route(explicit_capability="implementation")
    assert len(env.sink.events) == 2 and not env.sink.events[1].allowed


def test_authorization_audit_failure_fails_closed() -> None:
    env = Env()

    class Broken:
        def record(self, event) -> None:
            raise RuntimeError("down")

    env.router = DeterministicRouter(
        AuthorizationService(
            InMemoryMembershipRepository(
                (InitiativeMembership("user-1", "travel-platform", (Role.ANALYST,)),)
            ),
            POLICY,
            Broken(),
        )
    )
    with pytest.raises(AuthorizationAuditError):
        env.route(explicit_capability="investigation")


def test_membership_missing_or_disabled_is_denied() -> None:
    env = Env()
    env.router = DeterministicRouter(
        AuthorizationService(InMemoryMembershipRepository(), POLICY, env.sink)
    )
    d = env.route(explicit_capability="investigation")
    assert d.outcome is RoutingOutcome.DENIED


def test_telemetry_events_and_safe_metadata() -> None:
    env = Env()
    env.route(explicit_capability="investigation")
    env.route(workflow_ids=("code-impact",))
    env.route(content="secret customer text")
    env.route(explicit_capability="implementation")
    env.route(explicit_capability="nope")
    env.route(
        workflow_ids=("dup",),
        profile_=env.profile.model_copy(
            update={
                "routing": Routing.model_construct(
                    capabilities=env.profile.routing.capabilities,
                    workflows=(
                        WorkflowRoute(id="dup", capability=INV),
                        WorkflowRoute(id="dup", capability=IMPACT),
                    ),
                )
            }
        ),
    )
    assert env.telemetry.events == [
        "routing.explicit_selected",
        "routing.workflow_matched",
        "routing.unresolved",
        "routing.denied",
        "routing.invalid_request",
        "routing.configuration_error",
    ]
    text = repr(env.telemetry.metrics) + repr(env.telemetry.events)
    assert "secret customer text" not in text and "user-1" not in text


def test_telemetry_failure_does_not_change_outcome() -> None:
    ok = Env().route(explicit_capability="investigation")
    failing = Env(telemetry=FakeTelemetry(fail=True))
    assert failing.route(explicit_capability="investigation") == ok
    assert failing.route(explicit_capability="implementation").outcome is RoutingOutcome.DENIED
    assert (
        DeterministicRouter(Env().auth)
        .route(
            RoutingRequest(explicit_capability="investigation"),
            context=Env().context,
            profile=Env().profile,
            initiative_revision=1,
        )
        .capability
        is INV
    )


def test_results_are_deterministic() -> None:
    env = Env()
    assert len({repr(env.route(workflow_ids=("code-impact",))) for _ in range(5)}) == 1


# --- boundaries -------------------------------------------------------------------------------


def test_decision_and_request_leak_nothing_sensitive() -> None:
    names = {f.name for f in fields(RoutingDecision)}
    assert not names & {"content", "principal", "email", "arn", "endpoint", "prompt", "scopes"}
    assert "content=" not in repr(RoutingRequest(content="private prompt"))
    d = Env().route(content="private prompt")
    assert "private prompt" not in repr(d)


def test_no_model_a2a_classifier_or_registry_dependencies() -> None:
    package = Path(routing_pkg.__file__).parent
    forbidden = (
        "ModelProvider",
        "A2AClient",
        "AgentDelegator",
        "model_registry",
        "invoke_model",
        "delegate",
        "boto3",
        "bedrock",
        "classify(",
    )
    for path in package.glob("*.py"):
        text = path.read_text()
        for word in forbidden:
            assert word not in text, (path.name, word)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("ai_dlc.adapters", "ai_dlc.application.gateway"))


def test_sdk_does_not_import_routing_and_profile_extension_is_backward_compatible() -> None:
    harness = Path(routing_pkg.__file__).parents[1] / "agent_harness"
    assert all("application.routing" not in p.read_text() for p in harness.glob("*.py"))
    legacy = load_initiative_profile(EXAMPLES / "travel-platform.yaml")
    assert legacy.routing == Routing()
    assert Env().route(workflow_ids=("x",), profile_=legacy).outcome is RoutingOutcome.UNRESOLVED
    assert Env().route(explicit_capability="investigation", profile_=legacy).outcome is (
        RoutingOutcome.CONFIGURATION_ERROR
    )


# --- mixed workflow policy --------------------------------------------------------------------

MIXED = (
    WorkflowRoute(id="wf-on", capability=CODE),
    WorkflowRoute(id="wf-on2", capability=CODE),
    WorkflowRoute(id="wf-other", capability=IMPACT),
    WorkflowRoute(id="wf-off", capability=CODE, enabled=False),
)


def test_mixed_enabled_and_disabled_workflow_fails_closed() -> None:
    env = Env(workflows=MIXED)
    for ids in (("wf-off",), ("wf-on", "wf-off"), ("wf-off", "wf-on")):
        d = env.route(workflow_ids=ids)
        assert d.outcome is RoutingOutcome.UNRESOLVED and d.capability is None
        assert d.reason is RoutingReason.WORKFLOW_DISABLED and not d.classifier_candidate
    assert env.telemetry.events == ["routing.unresolved"] * 3


def test_multiple_enabled_workflows_same_capability_route() -> None:
    d = Env(workflows=MIXED).route(workflow_ids=("wf-on", "wf-on2"))
    assert d.outcome is RoutingOutcome.ROUTED and d.capability is CODE
    assert d.workflow_id == "wf-on"


def test_multiple_enabled_workflows_different_capabilities_are_ambiguous() -> None:
    d = Env(workflows=MIXED).route(workflow_ids=("wf-on", "wf-other"))
    assert d.reason is RoutingReason.AMBIGUOUS_WORKFLOW and d.classifier_candidate


def test_unknown_and_known_workflow_together_do_not_route() -> None:
    env = Env(workflows=MIXED)
    for ids in (("wf-on", "nope"), ("nope", "wf-on")):
        d = env.route(workflow_ids=ids)
        assert d.outcome is RoutingOutcome.UNRESOLVED and d.capability is None
        assert d.reason is RoutingReason.UNKNOWN_WORKFLOW
    # a disabled workflow wins over an unknown one, keeping the non-candidate outcome
    d = env.route(workflow_ids=("wf-off", "nope"))
    assert d.reason is RoutingReason.WORKFLOW_DISABLED and not d.classifier_candidate


def test_workflow_selection_order_does_not_change_outcome() -> None:
    env = Env(workflows=MIXED)
    a = env.route(workflow_ids=("wf-on", "wf-on2"))
    b = env.route(workflow_ids=("wf-on2", "wf-on"))
    assert (a.outcome, a.capability, a.reason) == (b.outcome, b.capability, b.reason)
    assert a.workflow_id == "wf-on" and b.workflow_id == "wf-on"  # lowest id, not request order


def test_explicit_capability_with_unrelated_disabled_workflow_still_routes() -> None:
    env = Env(workflows=MIXED)
    d = env.route(explicit_capability="investigation", workflow_ids=("wf-off",))
    assert d.outcome is RoutingOutcome.ROUTED and d.capability is INV
    denied = env.route(explicit_capability="implementation", workflow_ids=("wf-off",))
    assert denied.outcome is RoutingOutcome.DENIED


def test_mixed_workflow_with_unauthorized_capability_is_denied() -> None:
    env = Env(workflows=(WorkflowRoute(id="wf-impl", capability=IMPL),))
    assert env.route(workflow_ids=("wf-impl",)).outcome is RoutingOutcome.DENIED
