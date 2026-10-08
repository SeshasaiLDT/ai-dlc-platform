"""A2A 1.0 SDK adapters for trusted, terminal agent delegation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from math import isfinite
from uuid import uuid4

from a2a.client import Client, ClientCallContext, ClientConfig, ClientFactory
from a2a.client.errors import A2AClientTimeoutError, AgentCardResolutionError
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskStore
from a2a.types import (
    AgentCard,
    Artifact,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT
from a2a.utils.errors import (
    InvalidParamsError,
    InvalidRequestError,
    JSONParseError,
    MethodNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct, Value
from httpx import HTTPStatusError

from .lifecycle import LifecycleRunner
from .models import AgentContext, ExecutionError, ExecutionResult, ExecutionStatus, Invocation
from .ports import AgentLifecycle

_NAMESPACE = "ai_dlc"
_TERMINAL = {
    TaskState.TASK_STATE_COMPLETED: ExecutionStatus.SUCCEEDED,
    TaskState.TASK_STATE_FAILED: ExecutionStatus.FAILED,
    TaskState.TASK_STATE_CANCELED: ExecutionStatus.CANCELLED,
}


def _value(data: dict) -> Value:
    return json_format.ParseDict(data, Value())


def _dict(value: Value) -> dict:
    decoded = json_format.MessageToDict(value)
    if not isinstance(decoded, dict):
        raise ValueError("A2A data part must contain an object")
    return decoded


def _failure(
    context: AgentContext,
    status: ExecutionStatus,
    code: str,
    message: str,
    *,
    details: dict | None = None,
    metadata: dict | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        error=ExecutionError(code=code, message=message, details=details),
        metadata=metadata,
    )


def _remaining(context: AgentContext, timeout: float) -> float:
    if context.deadline is None:
        return timeout
    return min(timeout, max(0.0, (context.deadline - datetime.now(UTC)).total_seconds()))


class ConfiguredAgentDirectory:
    """Trusted Agent Cards; the injected policy filters discovery and resolution."""

    def __init__(
        self, cards: Mapping[str, AgentCard], allowed: Callable[[str, AgentContext], bool]
    ) -> None:
        self._cards: dict[str, AgentCard] = {}
        for name, card in cards.items():
            if not name.strip() or not isinstance(card, AgentCard):
                raise ValueError("valid agent IDs and official Agent Cards required")
            if not any(
                interface.protocol_version == PROTOCOL_VERSION_CURRENT
                and interface.protocol_binding == "JSONRPC"
                for interface in card.supported_interfaces
            ):
                raise ValueError("agent card must support A2A 1.0 JSON-RPC")
            copy = AgentCard()
            copy.CopyFrom(card)
            self._cards[name] = copy
        self._allowed = allowed

    def resolve(self, agent_id: str, *, context: AgentContext) -> AgentCard | None:
        if agent_id not in self._cards or not self._allowed(agent_id, context):
            return None
        card = AgentCard()
        card.CopyFrom(self._cards[agent_id])
        return card

    def discover(self, *, context: AgentContext) -> dict[str, AgentCard]:
        return {
            name: card
            for name in self._cards
            if (card := self.resolve(name, context=context)) is not None
        }


class A2AClient:
    """AgentDelegator backed by the official SDK client and trusted directory."""

    def __init__(
        self,
        directory: ConfiguredAgentDirectory,
        *,
        transport_timeout_seconds: float = 30.0,
        client_factory: Callable[[AgentCard], Client] | None = None,
        call_context_factory: Callable[[AgentContext], ClientCallContext],
    ) -> None:
        if (
            not isinstance(transport_timeout_seconds, (int, float))
            or isinstance(transport_timeout_seconds, bool)
            or not isfinite(transport_timeout_seconds)
            or transport_timeout_seconds <= 0
        ):
            raise ValueError("transport timeout must be positive and finite")
        self._directory = directory
        self._timeout = float(transport_timeout_seconds)
        self._factory = client_factory
        self._call_context_factory = call_context_factory

    async def delegate(
        self, agent_id: str, request: Invocation, *, context: AgentContext
    ) -> ExecutionResult:
        if not isinstance(request, Invocation) or not isinstance(context, AgentContext):
            raise TypeError("Invocation and trusted AgentContext required")
        if context.task_id is None:
            raise ValueError("trusted parent task ID required")
        if context.cancellation_requested:
            return _failure(context, ExecutionStatus.CANCELLED, "cancelled", "Delegation cancelled")
        remaining = _remaining(context, self._timeout)
        if remaining <= 0:
            return _failure(
                context, ExecutionStatus.TIMED_OUT, "deadline_exceeded", "Deadline exceeded"
            )
        card = self._directory.resolve(agent_id, context=context)
        if card is None:
            return _failure(context, ExecutionStatus.FAILED, "agent_not_found", "Agent unavailable")
        message_id = uuid4().hex
        lineage = {
            _NAMESPACE: {
                "parent_task_id": context.task_id,
                "initiative_id": context.initiative_id,
                "request_id": context.request_id,
                "correlation_id": context.correlation_id,
                "trace_id": context.trace_id,
                "session_id": context.session_id,
                "deadline": context.deadline.isoformat() if context.deadline else None,
            }
        }
        wire = SendMessageRequest(
            message=Message(
                message_id=message_id,
                context_id=context.session_id,
                role=Role.ROLE_USER,
                reference_task_ids=[context.task_id],
                parts=[Part(data=_value(request.model_dump(mode="json")))],
            ),
            metadata=json_format.ParseDict(lineage, Struct()),
        )

        async def exchange() -> ExecutionResult:
            factory = self._factory or ClientFactory(ClientConfig(streaming=False)).create
            client = factory(card)
            call_context = self._call_context_factory(context)
            if not isinstance(call_context, ClientCallContext):
                raise TypeError("trusted client call context required")
            call_context.timeout = remaining
            async with client:
                async for response in client.send_message(wire, context=call_context):
                    return self._parse(response, context)
            return _failure(context, ExecutionStatus.FAILED, "protocol_error", "Empty A2A response")

        task = asyncio.create_task(exchange())
        cancellation = (
            asyncio.create_task(context.cancellation_event.wait())
            if context.cancellation_event is not None
            else None
        )
        try:
            waiting = {task} | ({cancellation} if cancellation is not None else set())
            done, _ = await asyncio.wait(
                waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation is not None and cancellation in done:
                return _failure(
                    context,
                    ExecutionStatus.CANCELLED,
                    "cancelled",
                    "Delegation cancelled",
                    metadata={"delegation_message_id": message_id},
                )
            if task not in done:
                return _failure(
                    context,
                    ExecutionStatus.TIMED_OUT,
                    "deadline_exceeded",
                    "Deadline exceeded",
                    metadata={"delegation_message_id": message_id},
                )
            return await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, A2AClientTimeoutError):
                return _failure(
                    context,
                    ExecutionStatus.TIMED_OUT,
                    "remote_timeout",
                    "Remote request timed out",
                    metadata={"delegation_message_id": message_id},
                )
            if isinstance(exc, (MethodNotFoundError, UnsupportedOperationError)):
                code, message = "unsupported_a2a_method", "Unsupported A2A method"
            elif isinstance(exc, (InvalidRequestError, InvalidParamsError, JSONParseError)):
                code, message = "invalid_request", "Invalid A2A request"
            elif isinstance(exc, HTTPStatusError) and exc.response.status_code in (401, 403):
                code = (
                    "authentication_failed"
                    if exc.response.status_code == 401
                    else "authorization_failed"
                )
                message = "Remote access denied"
            elif isinstance(exc, AgentCardResolutionError) and exc.status_code in (401, 403):
                code = "authentication_failed" if exc.status_code == 401 else "authorization_failed"
                message = "Remote access denied"
            else:
                code, message = "remote_unavailable", "Remote agent unavailable"
            return _failure(
                context,
                ExecutionStatus.FAILED,
                code,
                message,
                metadata={"delegation_message_id": message_id},
            )
        finally:
            for pending in (task, cancellation):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(item for item in (task, cancellation) if item is not None), return_exceptions=True
            )

    @staticmethod
    def _parse(response: StreamResponse, context: AgentContext) -> ExecutionResult:
        if not isinstance(response, StreamResponse) or not response.HasField("task"):
            return _failure(
                context, ExecutionStatus.FAILED, "protocol_error", "Invalid A2A response"
            )
        task = response.task
        if not task.id:
            return _failure(
                context, ExecutionStatus.FAILED, "protocol_error", "Invalid A2A task identity"
            )
        try:
            state_name = TaskState.Name(task.status.state)
        except ValueError:
            return _failure(
                context, ExecutionStatus.FAILED, "protocol_error", "Invalid A2A task state"
            )
        metadata = {"remote_task_id": task.id, "remote_task_state": state_name}
        if task.status.state == TaskState.TASK_STATE_REJECTED:
            return _failure(
                context,
                ExecutionStatus.FAILED,
                "remote_rejected",
                "Remote task rejected",
                metadata=metadata,
            )
        if task.status.state not in _TERMINAL:
            return _failure(
                context,
                ExecutionStatus.FAILED,
                "remote_task_pending",
                "Remote task requires later retrieval",
                metadata=metadata,
            )
        if not task.artifacts or not task.artifacts[0].parts:
            if task.status.state == TaskState.TASK_STATE_FAILED:
                return _failure(
                    context,
                    ExecutionStatus.FAILED,
                    "remote_execution_failed",
                    "Remote execution failed",
                    metadata=metadata,
                )
            if task.status.state == TaskState.TASK_STATE_CANCELED:
                return _failure(
                    context,
                    ExecutionStatus.CANCELLED,
                    "remote_cancelled",
                    "Remote invocation cancelled",
                    metadata=metadata,
                )
        try:
            data = _dict(task.artifacts[0].parts[0].data)
            result = ExecutionResult.model_validate(data)
            if (
                result.request_id != context.request_id
                or result.correlation_id != context.correlation_id
                or result.trace_id != context.trace_id
                or (
                    result.status is not _TERMINAL[task.status.state]
                    and not (
                        task.status.state == TaskState.TASK_STATE_FAILED
                        and result.status is ExecutionStatus.TIMED_OUT
                    )
                )
            ):
                raise ValueError("result lineage or status mismatch")
            return result.model_copy(update={"metadata": {**(result.metadata or {}), **metadata}})
        except (IndexError, ValueError, TypeError, AttributeError):
            return _failure(
                context,
                ExecutionStatus.FAILED,
                "protocol_error",
                "Invalid A2A response",
                metadata=metadata,
            )


class A2AServerAdapter(AgentExecutor):
    """Official SDK executor; trusted resolver authenticates and binds context."""

    def __init__(
        self,
        agent_factory: Callable[[], AgentLifecycle],
        trusted_context: Callable[[RequestContext], AgentContext],
        *,
        runner: LifecycleRunner | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._trusted_context = trusted_context
        self._runner = runner or LifecycleRunner()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        trusted = self._trusted_context(context)
        if not isinstance(trusted, AgentContext) or trusted.task_id is None:
            raise ValueError("trusted execution context required")
        try:
            message = context.message
            if message is None or message.role != Role.ROLE_USER or len(message.parts) != 1:
                raise ValueError("invalid request")
            envelope = context.metadata.get(_NAMESPACE)
            if (
                not isinstance(envelope, dict)
                or any(
                    envelope.get(key) != value
                    for key, value in {
                        "initiative_id": trusted.initiative_id,
                        "request_id": trusted.request_id,
                        "correlation_id": trusted.correlation_id,
                        "trace_id": trusted.trace_id,
                        "session_id": trusted.session_id,
                    }.items()
                )
                or context.task_id != trusted.task_id
                or message.context_id != trusted.session_id
            ):
                raise ValueError("untrusted request lineage")
            if (
                len(message.reference_task_ids) != 1
                or envelope.get("parent_task_id") != message.reference_task_ids[0]
            ):
                raise ValueError("invalid parent task")
            claimed_deadline = envelope.get("deadline")
            if claimed_deadline is not None:
                if not isinstance(claimed_deadline, str):
                    raise ValueError("invalid deadline")
                parsed_deadline = datetime.fromisoformat(claimed_deadline)
                if (
                    parsed_deadline.tzinfo is None
                    or trusted.deadline is None
                    or trusted.deadline > parsed_deadline
                ):
                    raise ValueError("untrusted deadline")
            invocation = Invocation.model_validate(_dict(message.parts[0].data))
        except Exception:
            result = _failure(
                trusted, ExecutionStatus.FAILED, "invalid_request", "Invalid A2A request"
            )
        else:
            try:
                agent = self._agent_factory()
            except Exception:
                result = _failure(
                    trusted,
                    ExecutionStatus.FAILED,
                    "initialization_failed",
                    "Agent initialization failed",
                )
            else:
                try:
                    result = await self._runner.run(agent, invocation, context=trusted)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result = _failure(
                        trusted,
                        ExecutionStatus.FAILED,
                        "server_execution_failed",
                        "Agent execution failed",
                    )
        state = {
            ExecutionStatus.SUCCEEDED: TaskState.TASK_STATE_COMPLETED,
            ExecutionStatus.FAILED: TaskState.TASK_STATE_FAILED,
            ExecutionStatus.CANCELLED: TaskState.TASK_STATE_CANCELED,
            ExecutionStatus.TIMED_OUT: TaskState.TASK_STATE_FAILED,
        }[result.status]
        task = Task(
            id=trusted.task_id,
            context_id=trusted.session_id,
            status=TaskStatus(state=state),
            artifacts=[
                Artifact(
                    artifact_id="execution-result",
                    parts=[Part(data=_value(result.model_dump(mode="json")))],
                )
            ],
        )
        await event_queue.enqueue_event(task)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The SDK cancels execute; remote side effects are not confirmed stopped.
        trusted = self._trusted_context(context)
        if trusted.cancellation_event is not None:
            trusted.cancellation_event.set()


def build_a2a_handler(
    adapter: A2AServerAdapter, card: AgentCard, task_store: TaskStore
) -> DefaultRequestHandler:
    """Attach the adapter to the official SDK's request and task handling."""
    if not isinstance(card, AgentCard) or not any(
        interface.protocol_version == PROTOCOL_VERSION_CURRENT
        and interface.protocol_binding == "JSONRPC"
        for interface in card.supported_interfaces
    ):
        raise ValueError("A2A 1.0 JSON-RPC Agent Card required")
    if card.capabilities.streaming or card.capabilities.push_notifications:
        raise ValueError("terminal adapter cannot advertise streaming or push notifications")
    return DefaultRequestHandler(
        agent_executor=adapter, task_store=task_store, agent_card=card, validate_input_modes=True
    )
