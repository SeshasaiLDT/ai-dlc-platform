"""Agent-facing MCP tool client over trusted ToolProvider adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import TypeVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

from .models import AgentContext, ExecutionError, ExecutionResult, ExecutionStatus, Invocation
from .ports import ToolProvider

T = TypeVar("T")

_DENIED = {"PERMISSION_DENIED", "INVALID_SCOPE", "UNAUTHORIZED_OPERATION"}
_INVALID = {"INVALID_ARGUMENT", "INVALID_ARGUMENTS"}
_UNAVAILABLE = {"UPSTREAM_AUTH_CONFIGURATION", "TRANSIENT_UPSTREAM_FAILURE", "PROVIDER_UNAVAILABLE"}
_RETRYABLE = {"provider_unavailable", "tool_timeout"}
_MESSAGES = {
    "tool_not_found": "Tool unavailable",
    "unauthorized_operation": "Tool operation unauthorized",
    "approval_required": "Human approval required",
    "invalid_arguments": "Invalid tool arguments",
    "provider_unavailable": "Tool provider unavailable",
    "provider_execution_failed": "Tool provider failed",
    "tool_timeout": "Tool invocation timed out",
    "invocation_cancelled": "Tool invocation cancelled",
}


class ToolDiscoveryError(Exception):
    """Safe discovery failure; provider internals are never included."""


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


def _positive(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _local_references_only(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("$ref", "$dynamicRef") and (
                not isinstance(item, str) or not item.startswith("#/")
            ):
                return False
            if not _local_references_only(item):
                return False
    elif isinstance(value, list):
        return all(_local_references_only(item) for item in value)
    return True


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_seconds: float = 0.05
    max_delay_seconds: float = 0.5

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        _positive(self.base_delay_seconds, "base_delay_seconds")
        _positive(self.max_delay_seconds, "max_delay_seconds")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base_delay_seconds exceeds max_delay_seconds")


@dataclass(frozen=True, slots=True)
class _Tool:
    name: str
    description: str | None
    schema: dict[str, JsonValue]
    approval_required: bool
    retry_safe: bool
    provider: ToolProvider

    def public_definition(self) -> dict[str, JsonValue]:
        definition: dict[str, JsonValue] = {
            "name": self.name,
            "inputSchema": self.schema,
            "approvalRequired": self.approval_required,
        }
        if self.description is not None:
            definition["description"] = self.description
        return definition


class McpClient:
    """ToolProvider facade; governance stays inside each injected provider."""

    def __init__(
        self,
        providers: tuple[ToolProvider, ...],
        *,
        default_timeout_seconds: float = 10.0,
        tool_timeouts: Mapping[str, float] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one governed tool provider required")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be RetryPolicy")
        self._providers = tuple(providers)
        self._default_timeout = _positive(default_timeout_seconds, "default_timeout_seconds")
        self._tool_timeouts = {
            name: _positive(timeout, f"timeout for {name}")
            for name, timeout in (tool_timeouts or {}).items()
        }
        self._retry = retry_policy if retry_policy is not None else RetryPolicy()

    async def list_tools(self, *, context: AgentContext) -> tuple[dict[str, JsonValue], ...]:
        try:
            tools = await self._discover(context)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ToolDiscoveryError("Governed tool discovery unavailable") from None
        return tuple(tool.public_definition() for tool in tools.values())

    async def get_tool(self, name: str, *, context: AgentContext) -> dict[str, JsonValue] | None:
        return next(
            (tool for tool in await self.list_tools(context=context) if tool["name"] == name), None
        )

    async def invoke_tool(
        self, name: str, request: Invocation, *, context: AgentContext
    ) -> ExecutionResult:
        if not isinstance(request, Invocation) or not isinstance(context, AgentContext):
            raise TypeError("Invocation and trusted AgentContext required")
        try:
            tools = await self._discover(context)
        except _Cancelled:
            return self._result(context, "invocation_cancelled", ExecutionStatus.CANCELLED)
        except _TimedOut:
            return self._result(context, "tool_timeout", ExecutionStatus.TIMED_OUT)
        except asyncio.CancelledError:
            raise
        except PermissionError:
            return self._result(context, "unauthorized_operation")
        except Exception:
            return self._result(context, "provider_unavailable")
        tool = tools.get(name)
        if tool is None:
            return self._result(context, "tool_not_found")
        try:
            valid_arguments = Draft202012Validator(
                tool.schema, format_checker=Draft202012Validator.FORMAT_CHECKER
            ).is_valid(request.input)
        except Exception:
            return self._result(context, "provider_unavailable")
        if not valid_arguments:
            return self._result(context, "invalid_arguments")

        for attempt in range(self._retry.max_attempts):
            try:
                raw = await self._call(
                    tool.provider.invoke_tool(name, request, context=context),
                    context,
                    self._tool_timeouts.get(name, self._default_timeout),
                )
                result = self._normalize(raw, context)
            except _Cancelled:
                return self._result(context, "invocation_cancelled", ExecutionStatus.CANCELLED)
            except _TimedOut:
                return self._result(context, "tool_timeout", ExecutionStatus.TIMED_OUT)
            except asyncio.CancelledError:
                raise
            except PermissionError:
                return self._result(context, "unauthorized_operation")
            except Exception:
                result = self._result(context, "provider_execution_failed")

            if (
                result.status is ExecutionStatus.SUCCEEDED
                or not tool.retry_safe
                or not result.error.retryable
                or result.error.code not in _RETRYABLE
                or attempt + 1 >= self._retry.max_attempts
            ):
                return result
            delay = min(
                self._retry.max_delay_seconds,
                self._retry.base_delay_seconds * 2**attempt,
            )
            remaining = self._remaining(context)
            if remaining is not None and remaining <= delay:
                return result
            try:
                await self._backoff(delay, context)
            except _Cancelled:
                return self._result(context, "invocation_cancelled", ExecutionStatus.CANCELLED)
            except _TimedOut:
                return self._result(context, "tool_timeout", ExecutionStatus.TIMED_OUT)
        raise AssertionError("bounded retry loop did not return")

    async def _discover(self, context: AgentContext) -> dict[str, _Tool]:
        if not isinstance(context, AgentContext):
            raise TypeError("trusted AgentContext required")
        found: dict[str, _Tool] = {}
        for provider in self._providers:
            definitions = await self._call(
                provider.list_tools(context=context), context, self._default_timeout
            )
            for raw in definitions:
                tool = self._tool(raw, provider)
                if tool.name in found:
                    raise ToolDiscoveryError("Duplicate governed tool name")
                found[tool.name] = tool
        return found

    @staticmethod
    def _tool(raw: dict[str, JsonValue], provider: ToolProvider) -> _Tool:
        if not isinstance(raw, dict):
            raise ToolDiscoveryError("Invalid governed tool definition")
        name = raw.get("name")
        description = raw.get("description")
        schema = raw.get("inputSchema")
        approval = raw.get("approvalRequired", False)
        retry_safe = raw.get("retrySafe", False)
        if (
            not isinstance(name, str)
            or not name.strip()
            or (description is not None and not isinstance(description, str))
            or not isinstance(schema, dict)
            or type(approval) is not bool
            or type(retry_safe) is not bool
        ):
            raise ToolDiscoveryError("Invalid governed tool definition")
        try:
            safe_schema = json.loads(json.dumps(schema, allow_nan=False))
            if not _local_references_only(safe_schema):
                raise ToolDiscoveryError("External schema references unavailable")
            Draft202012Validator.check_schema(safe_schema)
        except (TypeError, ValueError, SchemaError):
            raise ToolDiscoveryError("Invalid governed tool schema") from None
        return _Tool(name, description, safe_schema, approval, retry_safe, provider)

    async def _call(self, operation: Awaitable[T], context: AgentContext, timeout: float) -> T:
        try:
            effective = self._effective_timeout(context, timeout)
        except Exception:
            if asyncio.iscoroutine(operation):
                operation.close()
            raise
        task = asyncio.ensure_future(operation)
        cancellation = (
            asyncio.create_task(context.cancellation_event.wait())
            if context.cancellation_event is not None
            else None
        )
        try:
            waiting = {task}
            if cancellation is not None:
                waiting.add(cancellation)
            done, _ = await asyncio.wait(
                waiting, timeout=effective, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation is not None and cancellation in done:
                cancellation.result()
            if context.cancellation_requested or (
                cancellation is not None and cancellation in done
            ):
                raise _Cancelled
            if task not in done:
                raise _TimedOut
            value = await task
            self._effective_timeout(context, timeout)
            return value
        finally:
            tasks = (task, cancellation) if cancellation is not None else (task,)
            for item in tasks:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _backoff(self, delay: float, context: AgentContext) -> None:
        self._effective_timeout(context, delay)
        event = context.cancellation_event
        if event is None:
            await asyncio.sleep(delay)
        else:
            try:
                await asyncio.wait_for(event.wait(), timeout=delay)
            except TimeoutError:
                pass
            else:
                raise _Cancelled
        self._effective_timeout(context, delay)

    @staticmethod
    def _remaining(context: AgentContext) -> float | None:
        if context.deadline is None:
            return None
        return (context.deadline - datetime.now(UTC)).total_seconds()

    def _effective_timeout(self, context: AgentContext, timeout: float) -> float:
        if context.cancellation_requested:
            raise _Cancelled
        remaining = self._remaining(context)
        if remaining is not None and remaining <= 0:
            raise _TimedOut
        return min(timeout, remaining) if remaining is not None else timeout

    @classmethod
    def _normalize(cls, raw: object, context: AgentContext) -> ExecutionResult:
        if not isinstance(raw, ExecutionResult) or (
            raw.correlation_id != context.correlation_id
            or raw.trace_id != context.trace_id
            or raw.request_id not in (None, context.request_id)
        ):
            return cls._result(context, "provider_execution_failed")
        if raw.status is ExecutionStatus.SUCCEEDED:
            return raw.model_copy(update={"request_id": context.request_id})
        if raw.status is ExecutionStatus.CANCELLED:
            return cls._result(context, "invocation_cancelled", ExecutionStatus.CANCELLED)
        if raw.status is ExecutionStatus.TIMED_OUT:
            return cls._result(context, "tool_timeout", ExecutionStatus.TIMED_OUT)
        code = raw.error.code.upper()
        if code in _DENIED:
            normalized = "unauthorized_operation"
        elif code == "APPROVAL_REQUIRED":
            normalized = "approval_required"
        elif code in _INVALID:
            normalized = "invalid_arguments"
        elif code == "UPSTREAM_TIMEOUT":
            normalized = "tool_timeout"
        elif code == "CANCELLED":
            return cls._result(context, "invocation_cancelled", ExecutionStatus.CANCELLED)
        elif code in _UNAVAILABLE:
            normalized = "provider_unavailable"
        else:
            normalized = "provider_execution_failed"
        return cls._result(
            context,
            normalized,
            ExecutionStatus.TIMED_OUT if normalized == "tool_timeout" else ExecutionStatus.FAILED,
            retryable=raw.error.retryable and normalized in _RETRYABLE,
        )

    @staticmethod
    def _result(
        context: AgentContext,
        code: str,
        status: ExecutionStatus = ExecutionStatus.FAILED,
        *,
        retryable: bool = False,
    ) -> ExecutionResult:
        return ExecutionResult(
            status=status,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            error=ExecutionError(code=code, message=_MESSAGES[code], retryable=retryable),
        )
