"""Offline control-component authorization, registry dispatch and hosting contracts."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_gateway import USER, A

from ai_dlc.adapters.agent_registry.in_memory import InMemoryAgentRegistryRepository
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.adapters.orchestrator.host import Dependencies, RuntimeApp
from ai_dlc.application.agent_harness import ExecutionResult, ExecutionStatus
from ai_dlc.application.agent_registry import AgentManifest, AgentRegistry
from ai_dlc.application.authorization import AuthorizationService, RoleGrant, RolePolicy
from ai_dlc.application.gateway.context import GatewayContextResolver
from ai_dlc.application.initiatives import InitiativeRegistry
from ai_dlc.application.orchestrator.service import Orchestrator, SingleAgentRequest
from ai_dlc.domain.identity import Capability, InitiativeMembership, Role, ToolPermission


def world(*, allowed=True, mode="ok"):
    initiatives = InitiativeRegistry(InMemoryInitiativeRepository(), InMemoryRegistryEventSink())
    initiatives.create(A)
    memberships = InMemoryMembershipRepository(
        (InitiativeMembership(USER.subject_id, A.initiative.id, (Role.DEVELOPER,)),)
    )
    roles = RolePolicy(
        (
            RoleGrant(
                Role.DEVELOPER,
                capabilities=frozenset({Capability.INVESTIGATION}) if allowed else frozenset(),
                tool_permissions=frozenset({ToolPermission.JIRA_READ}),
            ),
        )
    )
    registry = AgentRegistry(InMemoryAgentRegistryRepository())
    calls = []

    def transport(directory):
        async def delegate(agent_id, invocation, *, context):
            calls.append((directory.resolve(agent_id, context=context), context))
            if mode == "timeout":
                raise TimeoutError
            if mode == "failure":
                raise RuntimeError("secret")
            return ExecutionResult(
                status=ExecutionStatus.SUCCEEDED,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                trace_id=context.trace_id,
                output=invocation.input,
            )

        return SimpleNamespace(delegate=delegate)

    service = Orchestrator(
        registry,
        GatewayContextResolver(initiatives, memberships, roles),
        AuthorizationService(memberships, roles, InMemoryAuthorizationAuditSink()),
        transport,
    )
    return service, registry, calls


def register(registry, agent_id="new-agent", permissions=frozenset()):
    registry.register(
        AgentManifest(
            agent_id=agent_id,
            name=agent_id,
            description="Investigates",
            agent_version="1.0.0",
            a2a_endpoint=f"https://{agent_id}.example.invalid/a2a",
            capabilities=("investigation",),
            permissions=permissions,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )
    )


def request(**changes):
    data = dict(
        initiative_id=A.initiative.id,
        capability="investigation",
        invocation={"input": {"question": "hello"}},
    )
    data.update(changes)
    return SingleAgentRequest(**data)


def execute(service, **changes):
    return asyncio.run(service.execute(request(**changes), USER))


def test_new_registration_dispatches_registry_endpoint_with_trusted_context():
    service, registry, calls = world()
    assert execute(service).error.code == "agent_not_found"
    register(registry)
    assert execute(service).status == ExecutionStatus.SUCCEEDED
    card, context = calls[0]
    assert card.supported_interfaces[0].url == "https://new-agent.example.invalid/a2a"
    assert context.initiative_id == A.initiative.id
    assert context.principal == USER
    assert context.task_id and context.deadline


@pytest.mark.parametrize("initiative", ["missing", " "])
def test_invalid_initiative_cannot_dispatch(initiative):
    service, registry, calls = world()
    register(registry)
    assert execute(service, initiative_id=initiative).error.code == "authorization_failed"
    assert not calls


def test_denied_capability_and_permission_requirements():
    service, registry, calls = world(allowed=False)
    register(registry)
    assert execute(service).error.code == "authorization_failed"
    assert not calls
    service, registry, calls = world()
    register(registry, permissions=frozenset({"jira.write"}))
    assert execute(service).error.code == "agent_not_found"
    assert not calls
    with pytest.raises(ValidationError):
        request(permissions=["jira.write"])
    with pytest.raises(ValidationError):
        request(initiative_id=None)


def test_ambiguous_agents_fail_without_dispatch():
    service, registry, calls = world()
    register(registry, "one")
    register(registry, "two")
    assert execute(service).error.code == "ambiguous_agent"
    assert not calls


@pytest.mark.parametrize(
    "mode,code", [("timeout", "deadline_exceeded"), ("failure", "remote_unavailable")]
)
def test_transport_errors_are_sanitized(mode, code):
    service, registry, _ = world(mode=mode)
    register(registry)
    result = execute(service)
    assert result.error.code == code
    assert "secret" not in result.model_dump_json()


def test_http_entrypoint_authenticates_and_calls_real_service():
    service, registry, calls = world()
    register(registry)
    auth = SimpleNamespace(authenticate=lambda _: USER)
    app = RuntimeApp(Dependencies(auth, service))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runtime"
        ) as client:
            assert (await client.get("/ping")).status_code == 200
            assert (await client.post("/invocations", json={})).status_code == 401
            headers = {"Authorization": "Bearer test"}
            invalid = await client.post("/invocations", json={}, headers=headers)
            assert invalid.status_code == 400
            response = await client.post(
                "/invocations", json=request().model_dump(mode="json"), headers=headers
            )
            assert response.json()["status"] == "succeeded"

    asyncio.run(run())
    assert len(calls) == 1


def test_deployment_factory_is_required_and_builds_host(monkeypatch):
    import sys

    from ai_dlc.adapters.orchestrator.host import create_app

    service, _, _ = world()
    dependencies = Dependencies(SimpleNamespace(), service)
    module = SimpleNamespace(dependencies=lambda: dependencies)
    monkeypatch.setitem(sys.modules, "deployment_test", module)
    monkeypatch.setenv("AIDLC_ORCHESTRATOR_FACTORY", "deployment_test:dependencies")
    assert create_app().dependencies is dependencies
    monkeypatch.delenv("AIDLC_ORCHESTRATOR_FACTORY")
    with pytest.raises(KeyError):
        create_app()
