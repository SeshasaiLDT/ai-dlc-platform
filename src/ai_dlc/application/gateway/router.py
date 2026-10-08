"""Thin trusted dispatch to existing enterprise MCP-facing handlers."""

from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from ai_dlc.application.git_remote import GitRemoteMcpToolHandler
from ai_dlc.application.jira import JiraMcpToolHandler
from ai_dlc.application.servicenow import ServiceNowMcpToolHandler
from ai_dlc.application.tool_policy import ToolKind

from .catalog import GatewayCatalog
from .context import TrustedGatewayContext
from .discovery import GatewayDiscovery


@dataclass(frozen=True, slots=True)
class GatewayTelemetryEvent:
    correlation_id: str
    principal_id: str
    initiative_id: str
    action: str
    tool_name: str | None
    outcome: str
    latency_ms: int


class GatewayTelemetrySink(Protocol):
    def record(self, event: GatewayTelemetryEvent) -> None: ...


class UnknownGatewayToolError(Exception):
    """No arbitrary name-to-handler fallback exists."""


class GatewayRouter:
    def __init__(
        self,
        catalog: GatewayCatalog,
        discovery: GatewayDiscovery,
        jira: JiraMcpToolHandler,
        servicenow: ServiceNowMcpToolHandler,
        git: GitRemoteMcpToolHandler,
        telemetry: GatewayTelemetrySink,
    ) -> None:
        self._catalog = catalog
        self._discovery = discovery
        self._handlers = {
            ToolKind.JIRA: jira,
            ToolKind.SERVICENOW: servicenow,
            ToolKind.GIT: git,
        }
        self._telemetry = telemetry

    def list_tools(self, *, context: TrustedGatewayContext) -> tuple[dict[str, object], ...]:
        started = perf_counter()
        try:
            found = self._discovery.available_tools(context)
            result = tuple(item.public_definition() for item in found)
            outcome = "success"
            return result
        except Exception:
            outcome = "error"
            raise
        finally:
            self._record(context, "discovery", None, outcome, started)

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        context: TrustedGatewayContext,
    ) -> dict[str, object]:
        started = perf_counter()
        outcome = "error"
        try:
            tool = self._catalog.by_name.get(tool_name)
            if tool is None:
                raise UnknownGatewayToolError("unknown enterprise tool")
            if not isinstance(arguments, Mapping):
                raise TypeError("tool arguments must be an object")
            trusted_context = {
                ToolKind.JIRA: context.for_jira,
                ToolKind.SERVICENOW: context.for_servicenow,
                ToolKind.GIT: context.for_git,
            }[tool.domain]()
            result = self._handlers[tool.domain].invoke(
                tool_name, arguments, context=trusted_context
            )
            outcome = str(result.get("outcome", "error"))
            return result
        finally:
            self._record(context, "invoke", tool_name, outcome, started)

    def _record(
        self,
        context: TrustedGatewayContext,
        action: str,
        tool_name: str | None,
        outcome: str,
        started: float,
    ) -> None:
        auth = context.resolution.authorization
        self._telemetry.record(
            GatewayTelemetryEvent(
                context.resolution.correlation_id,
                auth.principal.subject_id,
                auth.initiative_id,
                action,
                tool_name,
                outcome,
                max(0, int((perf_counter() - started) * 1000)),
            )
        )
