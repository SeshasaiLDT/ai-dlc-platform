"""Adapt the existing governed Gateway facade to the shared ToolProvider port."""

import asyncio

from pydantic import JsonValue

from ai_dlc.application.agent_harness import (
    AgentContext,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
)

from .context import TrustedGatewayContext
from .runtime import GatewayRuntimeAccess


class GatewayToolProvider:
    """One trusted invocation binding; never resolves identity from tool arguments."""

    def __init__(self, access: GatewayRuntimeAccess, trusted: TrustedGatewayContext) -> None:
        self._access = access
        self._trusted = trusted

    def _check(self, context: AgentContext) -> None:
        resolution = self._trusted.resolution
        if (
            resolution.authorization != context.authorization
            or resolution.authorization.principal != context.principal
            or resolution.authorization.initiative_id != context.initiative_id
            or resolution.correlation_id != context.correlation_id
            or self._trusted.task_id != context.task_id
        ):
            raise PermissionError("trusted Gateway context mismatch")

    async def list_tools(self, *, context: AgentContext) -> tuple[dict[str, JsonValue], ...]:
        self._check(context)
        return await asyncio.to_thread(self._access.available_tools, context=self._trusted)

    async def invoke_tool(
        self, name: str, request: Invocation, *, context: AgentContext
    ) -> ExecutionResult:
        self._check(context)
        raw = await asyncio.to_thread(
            self._access.invoke, name, request.input, context=self._trusted
        )
        if not isinstance(raw, dict) or raw.get("correlation_id") != context.correlation_id:
            return self._failure(context)
        if raw.get("outcome") == "success" and isinstance(raw.get("data"), dict):
            try:
                return ExecutionResult(
                    status=ExecutionStatus.SUCCEEDED,
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    trace_id=context.trace_id,
                    output=raw["data"],
                )
            except (TypeError, ValueError):
                return self._failure(context)
        error = raw.get("error")
        if raw.get("outcome") == "error" and isinstance(error, dict):
            code = error.get("code")
            retryable = error.get("retryable", False)
            if isinstance(code, str) and code and type(retryable) is bool:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    trace_id=context.trace_id,
                    error=ExecutionError(
                        code=code, message="Governed tool failed", retryable=retryable
                    ),
                )
        return self._failure(context)

    @staticmethod
    def _failure(context: AgentContext) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            error=ExecutionError(code="provider_execution_failed", message="Governed tool failed"),
        )
