"""Bounded graphs and durable admission; never executes an agent."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_gateway import USER, A
from test_orchestrator import world

from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.orchestrator.host import Dependencies, RuntimeApp
from ai_dlc.adapters.planning.sqlite import SQLitePlanRepository
from ai_dlc.application.agent_registry import AgentManifest
from ai_dlc.application.authorization import AuthorizationService, RoleGrant, RolePolicy
from ai_dlc.application.gateway.context import GatewayContextResolver
from ai_dlc.application.planning import PlanError, PlanLimits, PlanningRequest, PlanningService
from ai_dlc.domain.identity import Capability, InitiativeMembership, Principal, Role


def step(name, capability="investigation", deps=(), **changes):
    return dict(
        step_id=name,
        capability=capability,
        dependencies=deps,
        invocation={"input": {}},
        expected_output={"type": "object"},
        **changes,
    )


def request(steps=None, **changes):
    data = dict(
        initiative_id=A.initiative.id,
        request_id="request-1",
        correlation_id="corr-1",
        steps=steps
        if steps is not None
        else [
            step("inspect"),
            step("verify", "verification", ("inspect",), upstream_outputs={"evidence": "inspect"}),
        ],
    )
    data.update(changes)
    return PlanningRequest(**data)


def setup(tmp_path, *, allowed=True, limits=None):
    orchestrator, registry, calls = world()
    memberships = InMemoryMembershipRepository(
        (InitiativeMembership(USER.subject_id, A.initiative.id, (Role.DEVELOPER,)),)
    )
    roles = RolePolicy(
        (RoleGrant(Role.DEVELOPER, capabilities=frozenset(Capability) if allowed else frozenset()),)
    )
    contexts = GatewayContextResolver(orchestrator.contexts._initiatives, memberships, roles)
    auth = AuthorizationService(memberships, roles, InMemoryAuthorizationAuditSink())
    registry.register(
        AgentManifest(
            agent_id="dynamic",
            name="dynamic",
            description="Capability agent",
            agent_version="1.0.0",
            a2a_endpoint="https://example.invalid/a2a",
            capabilities=tuple(c.value for c in Capability),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )
    )
    repository = SQLitePlanRepository(tmp_path / "plans.db")
    service = PlanningService(registry, contexts, auth, repository, limits=limits)
    return service, orchestrator, calls


def test_plan_is_explicit_persisted_and_fresh_adapter_can_load(tmp_path):
    service, _, calls = setup(tmp_path)
    plan = service.accept(request(), USER)
    assert [s.step_id for s in plan.steps] == ["inspect", "verify"]
    assert plan.steps[1].dependencies == ("inspect",)
    assert plan.steps[1].upstream_outputs == {"evidence": "inspect"}
    assert plan.steps[1].expected_output == {"type": "object"}
    assert plan.schema_version == "1.0.0" and plan.state == "accepted"
    assert plan.limits.nested_delegation is False
    fresh = SQLitePlanRepository(tmp_path / "plans.db")
    assert fresh.get(plan.plan_id) == plan
    assert service.load_for_execution(plan.plan_id, plan.initiative_id, USER) == plan
    assert not calls


def test_independent_steps_and_dependency_levels_are_deterministic(tmp_path):
    service, _, _ = setup(tmp_path)
    steps = [step("z", deps=("a",)), step("b"), step("a")]
    first = service.accept(request(steps), USER)
    second = service.accept(request(list(reversed(steps))), USER)
    assert [s.step_id for s in first.steps] == ["a", "b", "z"]
    assert first == second


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [step("a"), step("a")],
        [step("a", deps=("missing",))],
        [step("a", deps=("a",))],
        [step("a", deps=("b",)), step("b", deps=("a",))],
        [step("a", upstream_outputs={"x": "b"}), step("b")],
        [step("a", expected_outputs={})],
    ],
)
def test_invalid_graph_never_reaches_repository(tmp_path, steps):
    service, _, _ = setup(tmp_path)
    with pytest.raises(ValidationError):
        service.accept(request(steps), USER)
    with pytest.raises(PlanError, match="plan_not_found"):
        service.repository.get("unsaved")


@pytest.mark.parametrize(
    "limits,steps",
    [
        (PlanLimits(max_steps=1), [step("a"), step("b")]),
        (PlanLimits(max_delegations=1), [step("a"), step("b")]),
        (PlanLimits(max_depth=1), [step("a"), step("b", deps=("a",))]),
    ],
)
def test_server_limits_reject_before_persistence(tmp_path, limits, steps):
    service, _, _ = setup(tmp_path, limits=limits)
    with pytest.raises(ValueError):
        service.accept(request(steps), USER)


def test_recursive_delegation_and_schema_bypasses_rejected():
    for extra in ({"steps": []}, {"delegation_depth": 0}, {"endpoint": "https://evil"}):
        with pytest.raises(ValidationError):
            request([step("a", **extra)])
    with pytest.raises(ValidationError):
        PlanLimits(max_steps=65)
    with pytest.raises(ValidationError):
        PlanLimits(nested_delegation=True)
    for schema in ({"type": "invalid"}, {"$ref": "https://evil/schema"}):
        data = step("a")
        data["expected_output"] = schema
        with pytest.raises(ValidationError):
            request([data])


def test_authorization_ownership_and_registry_eligibility(tmp_path):
    service, _, _ = setup(tmp_path, allowed=False)
    with pytest.raises(PlanError, match="authorization_failed"):
        service.accept(request(), USER)
    service, _, _ = setup(tmp_path)
    with pytest.raises(PlanError, match="authorization_failed"):
        service.accept(request(initiative_id="other"), USER)
    plan = service.accept(request(), USER)
    for initiative, principal in [
        ("other", USER),
        (plan.initiative_id, Principal("other", "test")),
    ]:
        with pytest.raises(PlanError, match="authorization_failed"):
            service.load_for_execution(plan.plan_id, initiative, principal)
    with pytest.raises(PlanError, match="plan_not_found"):
        service.load_for_execution(plan, plan.initiative_id, USER)
    with pytest.raises(PlanError, match="plan_not_found"):
        service.load_for_execution("never-saved", plan.initiative_id, USER)
    service.registry = SimpleNamespace(discover=lambda **_: ())
    with pytest.raises(PlanError, match="agent_not_found"):
        service.accept(request(request_id="new"), USER)


def test_atomic_idempotency_and_conflict(tmp_path):
    service, _, _ = setup(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        plans = list(pool.map(lambda _: service.accept(request(), USER), range(4)))
    assert len({p.plan_id for p in plans}) == 1
    with pytest.raises(PlanError, match="plan_conflict"):
        service.accept(request(correlation_id="different"), USER)
    assert service.repository.get(plans[0].plan_id) == plans[0]


def test_storage_failure_cannot_produce_execution_eligibility(tmp_path):
    service, _, _ = setup(tmp_path)
    # Opening the DB cannot silently create an alternate storage location.
    service.repository.path = str(tmp_path / "missing-parent" / "plans.db")
    with pytest.raises(PlanError, match="plan_persistence_failed"):
        service.accept(request(), USER)
    with pytest.raises(PlanError, match="plan_persistence_failed"):
        service.load_for_execution("unsaved", A.initiative.id, USER)


def test_http_planning_wiring_and_single_agent_regression(tmp_path):
    service, orchestrator, calls = setup(tmp_path)
    app = RuntimeApp(
        Dependencies(SimpleNamespace(authenticate=lambda _: USER), orchestrator, service)
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        ) as c:
            response = await c.post(
                "/invocations",
                headers={"Authorization": "Bearer test"},
                json={"operation": "plan", "request": request().model_dump(mode="json")},
            )
            assert response.status_code == 200
            assert service.repository.get(response.json()["plan_id"]).state == "accepted"

    asyncio.run(run())
    assert not calls


def test_invalid_submission_does_not_insert_any_rows(tmp_path):
    import sqlite3

    service, _, _ = setup(tmp_path, limits=PlanLimits(max_steps=1))
    with pytest.raises(ValueError):
        service.accept(request(), USER)
    with sqlite3.connect(tmp_path / "plans.db") as db:
        assert db.execute("SELECT COUNT(*) FROM task_plans").fetchone()[0] == 0


def test_handoff_rechecks_authorization_and_storage_validation(tmp_path):
    import sqlite3

    service, _, _ = setup(tmp_path)
    plan = service.accept(request(), USER)
    denied, _, _ = setup(tmp_path, allowed=False)
    with pytest.raises(PlanError, match="authorization_failed"):
        denied.load_for_execution(plan.plan_id, plan.initiative_id, USER)
    with sqlite3.connect(tmp_path / "plans.db") as db:
        db.execute("UPDATE task_plans SET payload='{}'")
    with pytest.raises(PlanError, match="plan_persistence_failed"):
        service.load_for_execution(plan.plan_id, plan.initiative_id, USER)


def test_request_grants_and_bounds_cannot_override_trusted_policy():
    with pytest.raises(ValidationError):
        request(permissions=["verification"])
    with pytest.raises(ValidationError):
        request(limits={"max_steps": 1000})
    chain = [step(f"s{i}", deps=(f"s{i - 1}",) if i else ()) for i in range(17)]
    with pytest.raises(ValidationError):
        request(chain)
