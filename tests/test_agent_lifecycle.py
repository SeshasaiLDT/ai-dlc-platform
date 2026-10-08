"""Deterministic, offline lifecycle and trusted-context behavior."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from ai_dlc.application.agent_harness import (
    AgentContext,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    LifecycleRunner,
    create_agent_context,
)
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.identity import InitiativeMembership, Principal


def authorization(
    subject: str = "user-1", initiative: str = "initiative-1"
) -> ResolvedAuthorizationContext:
    principal = Principal(subject, "test")
    return ResolvedAuthorizationContext(
        principal, initiative, InitiativeMembership(subject, initiative)
    )


def context(**changes: object) -> AgentContext:
    values = dict(task_id="task-1", authorization=authorization(), request_id="request-1")
    values.update(changes)
    return create_agent_context(**values)


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.init_error: Exception | None = None
        self.execute_error: Exception | None = None
        self.shutdown_error: Exception | None = None
        self.result: ExecutionResult | None = None
        self.seen_context: AgentContext | None = None

    async def initialize(self) -> None:
        self.calls.append("initialize")
        if self.init_error:
            raise self.init_error

    async def execute(self, request: Invocation, *, context: AgentContext) -> ExecutionResult:
        self.calls.append("execute")
        self.seen_context = context
        if self.execute_error:
            raise self.execute_error
        return self.result or ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            output=request.input,
        )

    async def shutdown(self) -> None:
        self.calls.append("shutdown")
        if self.shutdown_error:
            raise self.shutdown_error


def run(agent: FakeAgent, ctx: AgentContext | None = None) -> ExecutionResult:
    return asyncio.run(
        LifecycleRunner().run(agent, Invocation(input={"value": 1}), context=ctx or context())
    )


def test_context_normalization_identity_and_immutable_identifiers() -> None:
    auth = authorization()
    source = {"task_id": "untrusted", "nested": {"initiative_id": "untrusted"}}
    ctx = context(authorization=auth, metadata=source)
    source["nested"]["initiative_id"] = "changed"
    assert ctx.task_id == "task-1"
    assert ctx.initiative_id == "initiative-1"
    assert ctx.principal is auth.principal
    assert ctx.authorization is auth
    assert ctx.request_id == ctx.correlation_id == ctx.session_id == "request-1"
    assert ctx.trace_id
    assert ctx.metadata["nested"]["initiative_id"] == "untrusted"
    with pytest.raises(TypeError):
        ctx.metadata["nested"]["initiative_id"] = "changed"
    with pytest.raises(FrozenInstanceError):
        ctx.task_id = "other"
    with pytest.raises(FrozenInstanceError):
        ctx.authorization = authorization("other")
    with pytest.raises(ValueError):
        context(task_id=" ")
    with pytest.raises(ValueError):
        context(correlation_id=" ")


def test_successful_lifecycle_and_result_lineage() -> None:
    agent = FakeAgent()
    ctx = context(correlation_id="corr-1", trace_id="trace-1")
    result = run(agent, ctx)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert (result.request_id, result.correlation_id, result.trace_id) == (
        "request-1",
        "corr-1",
        "trace-1",
    )
    assert result.output == {"value": 1}
    assert agent.seen_context is ctx
    assert agent.calls == ["initialize", "execute", "shutdown"]


@pytest.mark.parametrize(
    ("phase", "expected_code", "expected_calls"),
    [
        ("init", "initialization_failed", ["initialize", "shutdown"]),
        ("execute", "execution_failed", ["initialize", "execute", "shutdown"]),
    ],
)
def test_failures_are_structured_sanitized_and_cleaned_up(
    phase: str, expected_code: str, expected_calls: list[str]
) -> None:
    agent = FakeAgent()
    secret = "secret-should-not-leak"
    if phase == "init":
        agent.init_error = RuntimeError(secret)
    else:
        agent.execute_error = RuntimeError(secret)
    result = run(agent)
    assert result.status is ExecutionStatus.FAILED
    assert result.error.code == expected_code
    assert secret not in result.model_dump_json()
    assert result.request_id == "request-1"
    assert agent.calls == expected_calls


def test_invalid_completion_and_cleanup_failure() -> None:
    agent = FakeAgent()
    agent.result = ExecutionResult(status="succeeded", correlation_id="forged", trace_id="trace")
    result = run(agent)
    assert result.error.code == "invalid_result"
    assert agent.calls[-1] == "shutdown"

    agent = FakeAgent()
    agent.shutdown_error = RuntimeError("internal cleanup secret")
    result = run(agent)
    assert result.status is ExecutionStatus.FAILED
    assert result.error.code == "cleanup_failed"
    assert "secret" not in result.model_dump_json()

    agent.execute_error = RuntimeError("primary")
    result = run(agent)
    assert result.error.code == "execution_failed"
    assert result.metadata == {"cleanup_failed": True, "cleanup_error_code": "cleanup_failed"}


def test_shutdown_timeout_is_structured_and_cancels_local_cleanup() -> None:
    async def scenario() -> None:
        cancelled = asyncio.Event()

        class BlockingShutdown(FakeAgent):
            async def shutdown(self) -> None:
                self.calls.append("shutdown")
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        agent = BlockingShutdown()
        result = await LifecycleRunner(cleanup_timeout_seconds=0.02).run(
            agent, Invocation(input={}), context=context()
        )
        assert result.status is ExecutionStatus.FAILED
        assert result.error.code == "cleanup_timed_out"
        assert result.request_id == "request-1"
        assert cancelled.is_set()
        assert agent.calls == ["initialize", "execute", "shutdown"]

    asyncio.run(scenario())


@pytest.mark.parametrize("primary", ["execution_failed", "deadline_exceeded"])
def test_shutdown_timeout_preserves_primary_failure(primary: str) -> None:
    class BlockingShutdown(FakeAgent):
        async def shutdown(self) -> None:
            await asyncio.Event().wait()

    agent = BlockingShutdown()
    ctx = context()
    if primary == "execution_failed":
        agent.execute_error = RuntimeError("internal detail")
    else:
        ctx = context(deadline=datetime.now(UTC) - timedelta(seconds=1))
    result = asyncio.run(
        LifecycleRunner(cleanup_timeout_seconds=0.02).run(agent, Invocation(input={}), context=ctx)
    )
    assert result.error.code == primary
    assert result.metadata == {
        "cleanup_failed": True,
        "cleanup_error_code": "cleanup_timed_out",
    }


def test_shutdown_raised_timeout_error_is_not_a_cleanup_deadline() -> None:
    agent = FakeAgent()
    agent.shutdown_error = TimeoutError("adapter failure")
    result = run(agent)
    assert result.error.code == "cleanup_failed"


def test_external_cancellation_during_shutdown_propagates_and_cancels_cleanup() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class BlockingShutdown(FakeAgent):
            async def shutdown(self) -> None:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        task = asyncio.create_task(
            LifecycleRunner(cleanup_timeout_seconds=1).run(
                BlockingShutdown(), Invocation(input={}), context=context()
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_cleanup_timeout_must_be_positive_and_finite(value: float) -> None:
    with pytest.raises(ValueError):
        LifecycleRunner(cleanup_timeout_seconds=value)


def test_cancellation_before_execution_and_timeout_before_execution() -> None:
    event = asyncio.Event()
    event.set()
    agent = FakeAgent()
    result = run(agent, context(cancellation_event=event))
    assert (result.status, result.error.code) == (ExecutionStatus.CANCELLED, "cancelled")
    assert agent.calls == ["shutdown"]

    agent = FakeAgent()
    result = run(agent, context(deadline=datetime.now(UTC) - timedelta(seconds=1)))
    assert (result.status, result.error.code) == (ExecutionStatus.TIMED_OUT, "deadline_exceeded")
    assert agent.calls == ["shutdown"]


def test_cancellation_during_execution_waits_for_agent_cleanup() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        finished = asyncio.Event()
        signal = asyncio.Event()

        class BlockingAgent(FakeAgent):
            async def execute(
                self, request: Invocation, *, context: AgentContext
            ) -> ExecutionResult:
                self.calls.append("execute")
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()

        agent = BlockingAgent()
        ctx = context(cancellation_event=signal)
        task = asyncio.create_task(LifecycleRunner().run(agent, Invocation(input={}), context=ctx))
        await entered.wait()
        signal.set()
        result = await task
        assert result.status is ExecutionStatus.CANCELLED
        assert ctx.cancellation_requested
        assert finished.is_set()
        assert agent.calls == ["initialize", "execute", "shutdown"]

    asyncio.run(scenario())


def test_timeout_during_execution_cancels_operation_and_cleans_up() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        finished = asyncio.Event()

        class BlockingAgent(FakeAgent):
            async def execute(
                self, request: Invocation, *, context: AgentContext
            ) -> ExecutionResult:
                self.calls.append("execute")
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()

        agent = BlockingAgent()
        ctx = context(deadline=datetime.now(UTC) + timedelta(milliseconds=100))
        task = asyncio.create_task(LifecycleRunner().run(agent, Invocation(input={}), context=ctx))
        await entered.wait()
        result = await task
        assert (result.status, result.error.code) == (
            ExecutionStatus.TIMED_OUT,
            "deadline_exceeded",
        )
        assert finished.is_set()
        assert agent.calls == ["initialize", "execute", "shutdown"]

    asyncio.run(scenario())


def test_external_task_cancellation_is_not_swallowed() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        class BlockingAgent(FakeAgent):
            async def execute(
                self, request: Invocation, *, context: AgentContext
            ) -> ExecutionResult:
                self.calls.append("execute")
                entered.set()
                await asyncio.Event().wait()

        agent = BlockingAgent()
        task = asyncio.create_task(
            LifecycleRunner().run(agent, Invocation(input={}), context=context())
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.calls[-1] == "shutdown"

    asyncio.run(scenario())


def test_concurrent_invocations_have_independent_context_and_cancellation() -> None:
    async def scenario() -> None:
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        contexts = [
            context(task_id="task-a", request_id="request-a"),
            context(task_id="task-b", request_id="request-b"),
        ]

        class GatedAgent(FakeAgent):
            def __init__(self, index: int) -> None:
                super().__init__()
                self.index = index

            async def execute(
                self, request: Invocation, *, context: AgentContext
            ) -> ExecutionResult:
                entered[self.index].set()
                await release[self.index].wait()
                return await super().execute(request, context=context)

        agents = [GatedAgent(0), GatedAgent(1)]
        runner = LifecycleRunner()
        tasks = [
            asyncio.create_task(runner.run(agent, Invocation(input={}), context=ctx))
            for agent, ctx in zip(agents, contexts, strict=True)
        ]
        await asyncio.gather(*(event.wait() for event in entered))
        contexts[0].cancellation_event.set()
        release[1].set()
        first, second = await asyncio.gather(*tasks)
        assert first.status is ExecutionStatus.CANCELLED
        assert second.status is ExecutionStatus.SUCCEEDED
        assert second.request_id == "request-b"
        assert agents[1].seen_context is contexts[1]
        assert contexts[0].cancellation_event is not contexts[1].cancellation_event

    asyncio.run(scenario())
