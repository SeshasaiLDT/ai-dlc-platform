"""Per-invocation lifecycle execution without agent routing or persistence."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from math import isfinite
from typing import TypeVar

from .models import AgentContext, ExecutionError, ExecutionResult, ExecutionStatus, Invocation
from .ports import AgentLifecycle

T = TypeVar("T")


class _InvocationCancelled(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


class _CleanupTimedOut(Exception):
    pass


class LifecycleRunner:
    """Run one agent instance for one trusted invocation; owns no execution state."""

    def __init__(
        self, *, clock: Callable[[], datetime] | None = None, cleanup_timeout_seconds: float = 5.0
    ) -> None:
        if (
            isinstance(cleanup_timeout_seconds, bool)
            or not isinstance(cleanup_timeout_seconds, (int, float))
            or not isfinite(cleanup_timeout_seconds)
            or cleanup_timeout_seconds <= 0
        ):
            raise ValueError("cleanup_timeout_seconds must be a positive finite number")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cleanup_timeout_seconds = float(cleanup_timeout_seconds)

    async def run(
        self, agent: AgentLifecycle, request: Invocation, *, context: AgentContext
    ) -> ExecutionResult:
        if not isinstance(request, Invocation) or not isinstance(context, AgentContext):
            raise TypeError("Invocation and trusted AgentContext required")
        if context.task_id is None:
            raise ValueError("trusted task_id required")

        result: ExecutionResult | None = None
        phase = "initialization"
        try:
            await self._controlled(agent.initialize, context)
            phase = "execution"
            returned = await self._controlled(
                lambda: agent.execute(request, context=context), context
            )
            if not isinstance(returned, ExecutionResult) or (
                returned.correlation_id != context.correlation_id
                or returned.trace_id != context.trace_id
                or returned.request_id not in (None, context.request_id)
            ):
                result = self._failure(context, "invalid_result", "Invalid agent completion")
            else:
                result = returned.model_copy(update={"request_id": context.request_id})
        except _InvocationCancelled:
            result = self._terminal(
                context, ExecutionStatus.CANCELLED, "cancelled", "Invocation cancelled"
            )
        except _DeadlineExceeded:
            result = self._terminal(
                context, ExecutionStatus.TIMED_OUT, "deadline_exceeded", "Deadline exceeded"
            )
        except Exception:
            code = "initialization_failed" if phase == "initialization" else "execution_failed"
            result = self._failure(context, code, f"Agent {phase} failed")
        finally:
            try:
                await self._shutdown(agent)
            except _CleanupTimedOut:
                result = self._cleanup_failure(context, result, "cleanup_timed_out")
            except Exception:
                result = self._cleanup_failure(context, result, "cleanup_failed")

        assert result is not None
        return result

    async def _shutdown(self, agent: AgentLifecycle) -> None:
        task = asyncio.create_task(agent.shutdown())
        try:
            done, _ = await asyncio.wait({task}, timeout=self._cleanup_timeout_seconds)
            if task not in done:
                raise _CleanupTimedOut
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @classmethod
    def _cleanup_failure(
        cls, context: AgentContext, primary: ExecutionResult | None, code: str
    ) -> ExecutionResult:
        if primary is None or primary.status is ExecutionStatus.SUCCEEDED:
            message = (
                "Agent cleanup timed out" if code == "cleanup_timed_out" else "Agent cleanup failed"
            )
            return cls._failure(context, code, message)
        metadata = dict(primary.metadata or {})
        metadata.update(cleanup_failed=True, cleanup_error_code=code)
        return primary.model_copy(update={"metadata": metadata})

    async def _controlled(self, operation: Callable[[], Awaitable[T]], context: AgentContext) -> T:
        self._check(context)
        execution = asyncio.create_task(operation())
        cancellation = (
            asyncio.create_task(context.cancellation_event.wait())
            if context.cancellation_event is not None
            else None
        )
        try:
            waiting = {execution}
            if cancellation is not None:
                waiting.add(cancellation)
            remaining = self._remaining(context)
            done, _ = await asyncio.wait(
                waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation is not None and cancellation in done:
                raise _InvocationCancelled
            if execution not in done:
                raise _DeadlineExceeded
            value = await execution
            self._check(context)
            return value
        finally:
            tasks = (execution, cancellation) if cancellation is not None else (execution,)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _remaining(self, context: AgentContext) -> float | None:
        if context.deadline is None:
            return None
        return max(0.0, (context.deadline - self._clock()).total_seconds())

    def _check(self, context: AgentContext) -> None:
        if context.cancellation_requested:
            raise _InvocationCancelled
        if context.deadline is not None and context.deadline <= self._clock():
            raise _DeadlineExceeded

    @staticmethod
    def _terminal(
        context: AgentContext, status: ExecutionStatus, code: str, message: str
    ) -> ExecutionResult:
        return ExecutionResult(
            status=status,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            error=ExecutionError(code=code, message=message),
        )

    @classmethod
    def _failure(cls, context: AgentContext, code: str, message: str) -> ExecutionResult:
        return cls._terminal(context, ExecutionStatus.FAILED, code, message)
