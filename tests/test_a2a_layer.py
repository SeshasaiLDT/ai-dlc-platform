"""Offline A2A 1.0 SDK adapter contracts."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from a2a.client import ClientCallContext
from a2a.client.errors import A2AClientTimeoutError
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCard,
    AgentInterface,
    StreamResponse,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT
from a2a.utils.errors import InvalidParamsError, MethodNotFoundError
from httpx import HTTPStatusError, Request, Response

from ai_dlc.application.agent_harness import (
    A2AClient,
    A2AServerAdapter,
    ConfiguredAgentDirectory,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    build_a2a_handler,
    create_agent_context,
)
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.identity import InitiativeMembership, Principal


def context(subject="user-1", initiative="initiative-1", **changes):
    authorization = ResolvedAuthorizationContext(
        Principal(subject, "test"), initiative, InitiativeMembership(subject, initiative)
    )
    fields = dict(
        task_id="parent-1",
        authorization=authorization,
        request_id="request-1",
        correlation_id="correlation-1",
        trace_id="trace-1",
        session_id="session-1",
    )
    fields.update(changes)
    return create_agent_context(**fields)


def card():
    return AgentCard(
        name="echo",
        description="Echo agent",
        version="1.0",
        supported_interfaces=[
            AgentInterface(
                url="https://example.invalid/a2a",
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
    )


class EchoAgent:
    seen = None

    async def initialize(self):
        pass

    async def execute(self, request, *, context):
        self.seen = (request, context)
        return ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            output=request.input,
        )

    async def shutdown(self):
        pass


class Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class LocalClient:
    def __init__(self, adapter, trusted, *, mode="normal"):
        self.adapter = adapter
        self.trusted = trusted
        self.mode = mode
        self.request = None
        self.call_context = None
        self.closed = False
        self.started = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send_message(self, request, *, context):
        self.request = request
        self.call_context = context
        self.started.set()
        if isinstance(self.mode, Exception):
            raise self.mode
        if self.mode == "error":
            raise RuntimeError("provider secret")
        if self.mode == "block":
            await asyncio.Event().wait()
        if self.mode == "pending":
            yield StreamResponse(
                task=Task(
                    id="remote-pending-1",
                    context_id=request.message.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
            return
        server_context = RequestContext(
            ServerCallContext(),
            request=request,
            task_id=request.message.task_id,
            context_id=request.message.context_id,
        )
        queue = Queue()
        await self.adapter.execute(server_context, queue)
        yield StreamResponse(task=queue.events[0])


def setup(parent=None, *, mode="normal", allowed=None):
    parent = parent or context()
    agent = EchoAgent()

    def trusted(request):
        return create_agent_context(
            task_id=request.message.task_id,
            authorization=parent.authorization,
            request_id=parent.request_id,
            correlation_id=parent.correlation_id,
            trace_id=parent.trace_id,
            session_id=parent.session_id,
            deadline=parent.deadline,
        )

    adapter = A2AServerAdapter(lambda: agent, trusted)
    local = LocalClient(adapter, parent, mode=mode)
    directory = ConfiguredAgentDirectory(
        {"echo": card()}, allowed or (lambda name, ctx: ctx.initiative_id == "initiative-1")
    )
    client = A2AClient(
        directory,
        client_factory=lambda _: local,
        call_context_factory=lambda _: ClientCallContext(),
    )
    return client, directory, local, agent


def run(coro):
    return asyncio.run(coro)


def test_sdk_version_card_discovery_and_policy_filtering():
    client, directory, _, _ = setup()
    assert PROTOCOL_VERSION_CURRENT == "1.0"
    assert list(directory.discover(context=context())) == ["echo"]
    assert directory.discover(context=context(initiative="other")) == {}
    hidden = run(client.delegate("echo", Invocation(input={}), context=context(initiative="other")))
    unknown = run(client.delegate("missing", Invocation(input={}), context=context()))
    assert hidden.error.code == unknown.error.code == "agent_not_found"


def test_terminal_round_trip_serialization_lineage_and_parent_child():
    parent = context()
    client, _, local, agent = setup(parent)
    result = run(
        client.delegate(
            "echo",
            Invocation(input={"value": 3, "principal": "forged"}, metadata={"x": 1}),
            context=parent,
        )
    )
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output == {"value": 3, "principal": "forged"}
    assert (result.request_id, result.correlation_id, result.trace_id) == (
        parent.request_id,
        parent.correlation_id,
        parent.trace_id,
    )
    assert result.metadata["remote_task_id"] != parent.task_id
    assert local.request.message.reference_task_ids == [parent.task_id]
    assert local.request.message.task_id == result.metadata["remote_task_id"]
    assert local.request.message.context_id == parent.session_id
    assert agent.seen[1].authorization is parent.authorization
    assert agent.seen[1].principal is parent.principal
    assert agent.seen[1].initiative_id == parent.initiative_id
    assert local.call_context.timeout > 0
    assert local.closed


def test_server_rejects_forged_lineage_without_running_agent():
    parent = context()
    client, _, local, agent = setup(parent)
    run(client.delegate("echo", Invocation(input={}), context=parent))
    request = local.request
    request.metadata["ai_dlc"]["initiative_id"] = "other"

    async def scenario():
        server_context = RequestContext(
            ServerCallContext(),
            request=request,
            task_id=request.message.task_id,
            context_id=request.message.context_id,
        )
        queue = Queue()
        await local.adapter.execute(server_context, queue)
        return queue.events[0]

    task = run(scenario())
    assert task.status.state == TaskState.TASK_STATE_FAILED
    result = A2AClient._parse(StreamResponse(task=task), parent)
    assert result.error.code == "invalid_request"


def test_server_factory_failure_is_structured():
    parent = context()
    client, _, local, _ = setup(parent)

    def broken_factory():
        raise RuntimeError("internal secret")

    local.adapter._agent_factory = broken_factory
    result = run(client.delegate("echo", Invocation(input={}), context=parent))
    assert result.error.code == "initialization_failed"
    assert "secret" not in result.model_dump_json()


def test_pending_task_is_preserved_for_future_polling():
    client, _, _, _ = setup(mode="pending")
    result = run(client.delegate("echo", Invocation(input={}), context=context()))
    assert result.error.code == "remote_task_pending"
    assert result.metadata["remote_task_id"]
    assert result.metadata["remote_task_state"] == "TASK_STATE_WORKING"


def test_transport_failure_is_sanitized():
    client, _, _, _ = setup(mode="error")
    result = run(client.delegate("echo", Invocation(input={}), context=context()))
    assert result.error.code == "remote_unavailable"
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MethodNotFoundError("secret"), "unsupported_a2a_method"),
        (InvalidParamsError("secret"), "invalid_request"),
        (A2AClientTimeoutError("secret"), "remote_timeout"),
    ],
)
def test_typed_protocol_errors_are_normalized(error, expected):
    client, _, _, _ = setup(mode=error)
    result = run(client.delegate("echo", Invocation(input={}), context=context()))
    assert result.error.code == expected
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status", "code"), [(401, "authentication_failed"), (403, "authorization_failed")]
)
def test_transport_access_errors_are_sanitized(status, code):
    request = Request("POST", "https://example.invalid/a2a")
    response = Response(status, request=request)
    error = HTTPStatusError("secret", request=request, response=response)
    client, _, _, _ = setup(mode=error)
    result = run(client.delegate("echo", Invocation(input={}), context=context()))
    assert result.error.code == code
    assert "secret" not in result.model_dump_json()


def test_invalid_response_and_remote_execution_failure():
    trusted = context()
    invalid = StreamResponse(
        task=Task(id="remote-1", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
    )
    failed = StreamResponse(
        task=Task(id="remote-2", status=TaskStatus(state=TaskState.TASK_STATE_FAILED))
    )
    assert A2AClient._parse(invalid, trusted).error.code == "protocol_error"
    result = A2AClient._parse(failed, trusted)
    assert result.error.code == "remote_execution_failed"
    assert result.metadata["remote_task_id"] == "remote-2"


def test_deadline_and_transport_timeout():
    expired = context(deadline=datetime.now(UTC) - timedelta(seconds=1))
    client, _, local, _ = setup(expired)
    result = run(client.delegate("echo", Invocation(input={}), context=expired))
    assert result.status is ExecutionStatus.TIMED_OUT
    assert local.request is None

    future = context(deadline=datetime.now(UTC) + timedelta(minutes=1))
    client, _, local, _ = setup(future)
    result = run(client.delegate("echo", Invocation(input={}), context=future))
    assert result.status is ExecutionStatus.SUCCEEDED
    assert local.request.metadata["ai_dlc"]["deadline"] == future.deadline.isoformat()

    async def scenario():
        active = context()
        client, _, local, _ = setup(active, mode="block")
        client._timeout = 0.01
        result = await client.delegate("echo", Invocation(input={}), context=active)
        assert result.status is ExecutionStatus.TIMED_OUT
        assert result.metadata["delegation_message_id"]
        assert local.closed

    run(scenario())


def test_cooperative_and_external_cancellation():
    async def scenario():
        active = context()
        client, _, local, _ = setup(active, mode="block")
        running = asyncio.create_task(client.delegate("echo", Invocation(input={}), context=active))
        await local.started.wait()
        active.cancellation_event.set()
        result = await running
        assert result.status is ExecutionStatus.CANCELLED
        assert local.closed

        active = context()
        client, _, local, _ = setup(active, mode="block")
        running = asyncio.create_task(client.delegate("echo", Invocation(input={}), context=active))
        await local.started.wait()
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("CancelledError swallowed")
        assert local.closed

    run(scenario())


def test_concurrent_delegations_have_unique_children():
    async def scenario():
        parent = context()
        client, _, _, _ = setup(parent)
        results = await asyncio.gather(
            *(client.delegate("echo", Invocation(input={"n": n}), context=parent) for n in range(5))
        )
        assert {result.output["n"] for result in results} == set(range(5))
        assert len({result.metadata["remote_task_id"] for result in results}) == 5

    run(scenario())


def test_official_sdk_request_handler_round_trip():
    async def scenario():
        parent = context()
        _, directory, local, _ = setup(parent)
        handler = build_a2a_handler(local.adapter, card(), InMemoryTaskStore())

        class HandlerClient(LocalClient):
            async def send_message(self, request, *, context):
                self.request = request
                response = await handler.on_message_send(request, ServerCallContext())
                yield StreamResponse(task=response)

        client = A2AClient(
            directory,
            client_factory=lambda _: HandlerClient(local.adapter, parent),
            call_context_factory=lambda _: ClientCallContext(),
        )
        result = await client.delegate("echo", Invocation(input={"sdk": True}), context=parent)
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.output == {"sdk": True}

    run(scenario())
