"""Offline shared MCP client checks with governed provider fakes."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from support.enterprise import READ_TOOLS, EnterpriseWorld, read_arguments

from ai_dlc.application.agent_harness import (
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    McpClient,
    RetryPolicy,
    ToolDiscoveryError,
    create_agent_context,
)
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.application.gateway import GatewayToolProvider
from ai_dlc.domain.identity import InitiativeMembership, Principal, ToolPermission

SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string", "pattern": "^[A-Z]+$"}},
    "required": ["key"],
    "additionalProperties": False,
}


def context(
    *, subject: str = "user-1", initiative: str = "initiative-1", allowed: bool = True, **kwargs
):
    principal = Principal(subject, "test")
    authorization = ResolvedAuthorizationContext(
        principal,
        initiative,
        InitiativeMembership(subject, initiative),
        tool_permissions=frozenset({ToolPermission.JIRA_READ}) if allowed else frozenset(),
    )
    return create_agent_context(task_id="task-1", authorization=authorization, **kwargs)


def failure(ctx, code: str, *, retryable: bool = False):
    return ExecutionResult(
        status=ExecutionStatus.FAILED,
        correlation_id=ctx.correlation_id,
        trace_id=ctx.trace_id,
        error=ExecutionError(code=code, message="secret provider detail", retryable=retryable),
    )


class FakeProvider:
    def __init__(self, *, retry_safe: bool = False, name: str = "lookup") -> None:
        self.name = name
        self.retry_safe = retry_safe
        self.calls = []
        self.responses = []

    async def list_tools(self, *, context):
        if ToolPermission.JIRA_READ not in context.authorization.tool_permissions:
            return ()
        return (
            {
                "name": self.name,
                "description": "Read a record",
                "inputSchema": SCHEMA,
                "approvalRequired": False,
                "retrySafe": self.retry_safe,
                "internalEndpoint": "must-not-leak",
            },
        )

    async def invoke_tool(self, name, request, *, context):
        self.calls.append((name, request, context))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response(context)
        return ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            output={
                "key": request.input["key"],
                "principal": context.principal.subject_id,
                "initiative": context.initiative_id,
                "task": context.task_id,
            },
        )


def invoke(client, ctx=None, name="lookup", arguments=None):
    return asyncio.run(
        client.invoke_tool(
            name,
            Invocation(input=arguments if arguments is not None else {"key": "ABC"}),
            context=ctx or context(),
        )
    )


def test_discovery_metadata_and_permission_filtering() -> None:
    client = McpClient((FakeProvider(), FakeProvider(name="other")))
    tools = asyncio.run(client.list_tools(context=context()))
    assert [item["name"] for item in tools] == ["lookup", "other"]
    assert tools[0]["description"] == "Read a record"
    assert tools[0]["inputSchema"] == SCHEMA
    assert "internalEndpoint" not in str(tools)
    assert asyncio.run(client.get_tool("lookup", context=context())) == tools[0]
    assert asyncio.run(client.list_tools(context=context(allowed=False))) == ()
    assert asyncio.run(client.get_tool("lookup", context=context(allowed=False))) is None


def test_success_propagates_trusted_identity_and_lineage() -> None:
    provider = FakeProvider()
    ctx = context(
        subject="person-2",
        initiative="initiative-2",
        request_id="request-2",
        correlation_id="corr-2",
        trace_id="trace-2",
    )
    result = invoke(McpClient((provider,)), ctx)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output == {
        "key": "ABC",
        "principal": "person-2",
        "initiative": "initiative-2",
        "task": "task-1",
    }
    assert (result.request_id, result.correlation_id, result.trace_id) == (
        "request-2",
        "corr-2",
        "trace-2",
    )
    assert provider.calls[0][2] is ctx


def test_arguments_are_validated_before_governed_invocation() -> None:
    provider = FakeProvider()
    client = McpClient((provider,))
    for arguments in ({}, {"key": "lower"}, {"key": "ABC", "principal": "forged"}):
        result = invoke(client, arguments=arguments)
        assert result.error.code == "invalid_arguments"
    assert provider.calls == []


def test_hidden_or_unknown_tool_reveals_no_metadata() -> None:
    provider = FakeProvider()
    client = McpClient((provider,))
    assert invoke(client, context(allowed=False)).error.code == "tool_not_found"
    assert invoke(client, name="other").error.code == "tool_not_found"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("PERMISSION_DENIED", "unauthorized_operation"),
        ("APPROVAL_REQUIRED", "approval_required"),
        ("INVALID_ARGUMENT", "invalid_arguments"),
        ("TRANSIENT_UPSTREAM_FAILURE", "provider_unavailable"),
        ("UPSTREAM_TIMEOUT", "tool_timeout"),
        ("other_private_code", "provider_execution_failed"),
    ],
)
def test_provider_errors_are_normalized_without_details(provider_code, expected) -> None:
    provider = FakeProvider()
    provider.responses = [lambda ctx: failure(ctx, provider_code, retryable=True)]
    result = invoke(McpClient((provider,)), arguments={"key": "ABC"})
    assert result.error.code == expected
    assert "secret" not in result.model_dump_json()
    if expected == "tool_timeout":
        assert result.status is ExecutionStatus.TIMED_OUT
    if expected in {"unauthorized_operation", "approval_required", "invalid_arguments"}:
        assert result.error.retryable is False


def test_provider_exception_is_sanitized() -> None:
    provider = FakeProvider()
    provider.responses = [RuntimeError("token=secret")]
    result = invoke(McpClient((provider,)))
    assert result.error.code == "provider_execution_failed"
    assert "secret" not in result.model_dump_json()


def test_mismatched_provider_lineage_is_rejected() -> None:
    provider = FakeProvider()
    provider.responses = [
        lambda ctx: ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            correlation_id="forged",
            trace_id=ctx.trace_id,
            output={"key": "ABC"},
        )
    ]
    result = invoke(McpClient((provider,)))
    assert result.error.code == "provider_execution_failed"


def test_tool_timeout_and_context_deadline() -> None:
    async def scenario() -> None:
        stopped = asyncio.Event()

        class BlockingProvider(FakeProvider):
            async def invoke_tool(self, name, request, *, context):
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        client = McpClient(
            (BlockingProvider(),),
            default_timeout_seconds=1,
            tool_timeouts={"lookup": 0.02},
        )
        result = await client.invoke_tool(
            "lookup", Invocation(input={"key": "ABC"}), context=context()
        )
        assert (result.status, result.error.code) == (ExecutionStatus.TIMED_OUT, "tool_timeout")
        assert stopped.is_set()
        expired = context(deadline=datetime.now(UTC) - timedelta(seconds=1))
        result = await client.invoke_tool(
            "lookup", Invocation(input={"key": "ABC"}), context=expired
        )
        assert result.status is ExecutionStatus.TIMED_OUT

    asyncio.run(scenario())


def test_cancellation_before_and_during_invocation() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        stopped = asyncio.Event()

        class BlockingProvider(FakeProvider):
            async def invoke_tool(self, name, request, *, context):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        provider = BlockingProvider()
        client = McpClient((provider,))
        signal = asyncio.Event()
        signal.set()
        cancelled = context(cancellation_event=signal)
        result = await client.invoke_tool(
            "lookup", Invocation(input={"key": "ABC"}), context=cancelled
        )
        assert result.status is ExecutionStatus.CANCELLED
        assert not entered.is_set()

        signal.clear()
        task = asyncio.create_task(
            client.invoke_tool("lookup", Invocation(input={"key": "ABC"}), context=cancelled)
        )
        await entered.wait()
        signal.set()
        result = await task
        assert (result.status, result.error.code) == (
            ExecutionStatus.CANCELLED,
            "invocation_cancelled",
        )
        assert stopped.is_set()

    asyncio.run(scenario())


def test_external_task_cancellation_is_not_swallowed() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        stopped = asyncio.Event()

        class BlockingProvider(FakeProvider):
            async def invoke_tool(self, name, request, *, context):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        task = asyncio.create_task(
            McpClient((BlockingProvider(),)).invoke_tool(
                "lookup", Invocation(input={"key": "ABC"}), context=context()
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(scenario())


def test_retries_only_explicitly_safe_and_retryable_failures() -> None:
    safe = FakeProvider(retry_safe=True)
    safe.responses = [lambda ctx: failure(ctx, "TRANSIENT_UPSTREAM_FAILURE", retryable=True)]
    policy = RetryPolicy(max_attempts=2, base_delay_seconds=0.001, max_delay_seconds=0.001)
    result = invoke(McpClient((safe,), retry_policy=policy))
    assert result.status is ExecutionStatus.SUCCEEDED
    assert len(safe.calls) == 2

    write = FakeProvider(retry_safe=False, name="write")
    write.responses = [lambda ctx: failure(ctx, "TRANSIENT_UPSTREAM_FAILURE", retryable=True)]
    result = invoke(McpClient((write,), retry_policy=policy), name="write")
    assert result.error.code == "provider_unavailable"
    assert len(write.calls) == 1

    denied = FakeProvider(retry_safe=True)
    denied.responses = [lambda ctx: failure(ctx, "APPROVAL_REQUIRED", retryable=True)]
    result = invoke(McpClient((denied,), retry_policy=policy))
    assert result.error.code == "approval_required"
    assert len(denied.calls) == 1


def test_concurrent_invocations_keep_contexts_isolated() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        client = McpClient((provider,))
        first = context(subject="a", initiative="one", correlation_id="c1")
        second = context(subject="b", initiative="two", correlation_id="c2")
        results = await asyncio.gather(
            client.invoke_tool("lookup", Invocation(input={"key": "ABC"}), context=first),
            client.invoke_tool("lookup", Invocation(input={"key": "XYZ"}), context=second),
        )
        assert [result.output["principal"] for result in results] == ["a", "b"]
        assert [result.output["initiative"] for result in results] == ["one", "two"]
        assert [result.correlation_id for result in results] == ["c1", "c2"]

    asyncio.run(scenario())


def test_duplicate_or_external_schema_fails_closed() -> None:
    client = McpClient((FakeProvider(), FakeProvider()))
    with pytest.raises(ToolDiscoveryError):
        asyncio.run(client.list_tools(context=context()))

    class ExternalSchema(FakeProvider):
        async def list_tools(self, *, context):
            return ({"name": "lookup", "inputSchema": {"$ref": "https://example.com/schema"}},)

    with pytest.raises(ToolDiscoveryError):
        asyncio.run(McpClient((ExternalSchema(),)).list_tools(context=context()))

    class MissingLocalReference(FakeProvider):
        async def list_tools(self, *, context):
            return ({"name": "lookup", "inputSchema": {"$ref": "#/$defs/missing"}},)

    malformed = invoke(McpClient((MissingLocalReference(),)))
    assert malformed.error.code == "provider_unavailable"


def test_existing_gateway_path_is_reused_and_bound_to_trusted_context() -> None:
    world = EnterpriseWorld()
    trusted = world.context()
    ctx = create_agent_context(
        task_id="task-one",
        authorization=trusted.resolution.authorization,
        request_id="request-one",
        correlation_id="contract-trace",
        trace_id="trace-one",
    )
    client = McpClient((GatewayToolProvider(world.runtime, trusted),))

    async def exercise():
        tools = await client.list_tools(context=ctx)
        assert READ_TOOLS["jira"] in {tool["name"] for tool in tools}
        return await client.invoke_tool(
            READ_TOOLS["jira"], Invocation(input=read_arguments("jira")), context=ctx
        )

    result = asyncio.run(exercise())
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.request_id == "request-one"
    assert world.jira.calls[-1].context.correlation_id == "contract-trace"

    other = context(correlation_id="contract-trace")
    mismatch = invoke(client, other, READ_TOOLS["jira"], read_arguments("jira"))
    assert mismatch.error.code == "unauthorized_operation"
