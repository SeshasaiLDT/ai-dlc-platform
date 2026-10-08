"""Structural interfaces for agent-owned logic and injected capabilities."""

from typing import Protocol

from pydantic import JsonValue

from .models import (
    AgentContext,
    ApprovalIntent,
    ApprovalReference,
    ExecutionError,
    ExecutionResult,
    Invocation,
    ValidationResult,
)


class AgentLifecycle(Protocol):
    async def initialize(self) -> None: ...

    async def execute(self, request: Invocation, *, context: AgentContext) -> ExecutionResult: ...

    async def shutdown(self) -> None: ...


class ToolProvider(Protocol):
    async def list_tools(self, *, context: AgentContext) -> tuple[dict[str, JsonValue], ...]: ...

    async def invoke_tool(
        self, name: str, request: Invocation, *, context: AgentContext
    ) -> ExecutionResult: ...


class ModelProvider(Protocol):
    async def invoke_model(
        self, role: str, request: Invocation, *, context: AgentContext
    ) -> ExecutionResult: ...


class AgentDelegator(Protocol):
    async def delegate(
        self, agent_id: str, request: Invocation, *, context: AgentContext
    ) -> ExecutionResult: ...


class Validator(Protocol):
    def validate(self, request: Invocation, *, context: AgentContext) -> ValidationResult: ...


class ApprovalProvider(Protocol):
    async def request_approval(
        self, intent: ApprovalIntent, *, context: AgentContext
    ) -> ApprovalReference: ...

    async def get_approval_status(
        self, approval_id: str, *, context: AgentContext
    ) -> ApprovalReference: ...


class TelemetryProvider(Protocol):
    def record_event(self, name: str, *, context: AgentContext) -> None: ...

    def record_metric(self, name: str, value: float, *, context: AgentContext) -> None: ...

    def record_error(self, error: ExecutionError, *, context: AgentContext) -> None: ...
