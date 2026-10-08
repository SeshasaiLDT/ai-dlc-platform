"""Offline contract checks for the shared agent interface surface."""

import asyncio
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from ai_dlc.application.agent_harness import (
    INTERFACE_VERSION,
    AgentContext,
    AgentDelegator,
    AgentLifecycle,
    ApprovalIntent,
    ApprovalProvider,
    ApprovalReference,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    ModelProvider,
    TelemetryProvider,
    ToolProvider,
    ValidationResult,
    Validator,
)
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.identity import InitiativeMembership, Principal


def context(**overrides: object) -> AgentContext:
    principal = Principal("user-1", "test")
    auth = ResolvedAuthorizationContext(
        principal, "initiative-1", InitiativeMembership("user-1", "initiative-1")
    )
    values = dict(
        request_id="request-1",
        correlation_id="correlation-1",
        session_id="session-1",
        trace_id="trace-1",
        authorization=auth,
    )
    values.update(overrides)
    return AgentContext(**values)


def test_required_trusted_context_and_immutable_identity() -> None:
    ctx = context(metadata={"source": "test"}, deadline=datetime.now(UTC))
    assert ctx.principal is ctx.authorization.principal
    assert ctx.metadata["source"] == "test"
    with pytest.raises(TypeError):
        ctx.metadata["source"] = "changed"
    with pytest.raises(ValueError):
        context(request_id=" ")
    with pytest.raises(ValueError):
        context(deadline=datetime.now())
    with pytest.raises(TypeError):
        context(authorization=Principal("other", "test"))
    with pytest.raises(ValidationError):
        context(metadata={1: "invalid key"})


def test_json_boundaries_and_optional_metadata() -> None:
    request = Invocation(input={"items": [1, True, None]})
    assert request.metadata is None
    assert json.loads(request.model_dump_json())["input"] == {"items": [1, True, None]}
    with pytest.raises(ValidationError):
        Invocation(input={"bad": object()})
    with pytest.raises(ValueError):
        Invocation(input={"bad": float("nan")})
    with pytest.raises(ValidationError):
        Invocation(input={}, metadata={"bad": object()})


def test_result_error_and_timeout_contract() -> None:
    result = ExecutionResult(
        status=ExecutionStatus.TIMED_OUT,
        correlation_id="correlation-1",
        trace_id="trace-1",
        error=ExecutionError(code="deadline_exceeded", message="Deadline elapsed"),
    )
    assert json.loads(result.model_dump_json())["status"] == "timed_out"
    assert result.metadata is None
    with pytest.raises(ValidationError):
        ExecutionResult(status="failed", correlation_id="c", trace_id="t")
    with pytest.raises(ValidationError):
        ExecutionResult(
            status="succeeded",
            correlation_id="c",
            trace_id="t",
            error=ExecutionError(code="x", message="error"),
        )


def test_fake_agent_and_tool_provider_conform_to_protocols() -> None:
    class FakeAgent:
        async def initialize(self) -> None:
            pass

        async def execute(self, request: Invocation, *, context: AgentContext) -> ExecutionResult:
            return ExecutionResult(
                status=ExecutionStatus.SUCCEEDED,
                correlation_id=context.correlation_id,
                trace_id=context.trace_id,
                output=request.input,
            )

        async def shutdown(self) -> None:
            pass

    class FakeTools:
        async def list_tools(self, *, context: AgentContext) -> tuple[dict[str, JsonValue], ...]:
            return ()

        async def invoke_tool(
            self, name: str, request: Invocation, *, context: AgentContext
        ) -> ExecutionResult:
            return await FakeAgent().execute(request, context=context)

    async def exercise(agent: AgentLifecycle, tools: ToolProvider) -> None:
        await agent.initialize()
        result = await agent.execute(Invocation(input={"ok": True}), context=context())
        assert result.output == {"ok": True}
        assert await tools.list_tools(context=context()) == ()
        assert (await tools.invoke_tool("demo", Invocation(input={}), context=context())).status
        await agent.shutdown()

    asyncio.run(exercise(FakeAgent(), FakeTools()))


def test_approval_and_validation_reuse_existing_status_contract() -> None:
    intent = ApprovalIntent(request_key="request-1", operation="write", logical_target={})
    assert json.loads(intent.model_dump_json())["logical_target"] == {}
    reference = ApprovalReference(approval_id="approval-1", status="pending")
    assert reference.status.value == "pending"
    with pytest.raises(ValidationError):
        ApprovalReference(approval_id="approval-1", status="granted")
    assert ValidationResult(valid=True).errors == ()
    with pytest.raises(ValidationError):
        ValidationResult(valid=False)


def test_io_ports_are_async_and_local_ports_are_sync() -> None:
    for port, methods in (
        (AgentLifecycle, ("initialize", "execute", "shutdown")),
        (ToolProvider, ("list_tools", "invoke_tool")),
        (ModelProvider, ("invoke_model",)),
        (AgentDelegator, ("delegate",)),
        (ApprovalProvider, ("request_approval", "get_approval_status")),
    ):
        assert all(inspect.iscoroutinefunction(getattr(port, name)) for name in methods)
    assert not inspect.iscoroutinefunction(Validator.validate)
    assert all(
        not inspect.iscoroutinefunction(getattr(TelemetryProvider, name))
        for name in ("record_event", "record_metric", "record_error")
    )


def test_version_and_agent_agnostic_imports() -> None:
    assert INTERFACE_VERSION == "1.3.0"
    package = Path(__file__).parents[1] / "src/ai_dlc/application/agent_harness"
    source = "\n".join(path.read_text() for path in package.glob("*.py"))
    for forbidden in (
        "ai_dlc.application.jira",
        "ai_dlc.application.servicenow",
        "boto3",
        "langchain",
    ):
        assert forbidden not in source
