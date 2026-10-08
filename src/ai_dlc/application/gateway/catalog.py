"""Deterministic enterprise catalog derived from existing MCP-facing handlers."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ai_dlc.application.git_remote import GIT_REMOTE_MCP_TOOLS, GitRemoteMcpToolHandler
from ai_dlc.application.jira import JIRA_MCP_TOOLS, JiraMcpToolHandler
from ai_dlc.application.servicenow import SERVICENOW_MCP_TOOLS, ServiceNowMcpToolHandler
from ai_dlc.application.tool_policy import (
    ToolKind,
    ToolOperationRisk,
    operation_risk,
    required_permission,
)
from ai_dlc.application.tool_policy.operations import ToolOperation
from ai_dlc.domain.identity import ToolPermission


@dataclass(frozen=True, slots=True)
class GatewayTool:
    name: str
    domain: ToolKind
    operation: ToolOperation
    risk: ToolOperationRisk
    permission: ToolPermission
    input_schema: Mapping[str, object]

    def public_definition(self, *, approval_required: bool = False) -> dict[str, object]:
        return {
            "name": self.name,
            "inputSchema": dict(self.input_schema),
            "approvalRequired": approval_required,
        }


class GatewayCatalog:
    def __init__(self, tools: tuple[GatewayTool, ...]) -> None:
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate enterprise tool name")
        self.tools = tuple(sorted(tools, key=lambda tool: tool.name))
        self.by_name = MappingProxyType({tool.name: tool for tool in self.tools})
        serialized = json.dumps(
            [(tool.name, tool.input_schema) for tool in self.tools],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.version = hashlib.sha256(serialized).hexdigest()[:16]

    @classmethod
    def from_handlers(cls) -> "GatewayCatalog":
        sources = (
            (ToolKind.JIRA, JIRA_MCP_TOOLS, JiraMcpToolHandler.definitions()),
            (ToolKind.SERVICENOW, SERVICENOW_MCP_TOOLS, ServiceNowMcpToolHandler.definitions()),
            (ToolKind.GIT, GIT_REMOTE_MCP_TOOLS, GitRemoteMcpToolHandler.definitions()),
        )
        tools = []
        for domain, operations, definitions in sources:
            by_name = {item["name"]: item for item in definitions}
            if len(by_name) != len(definitions) or set(by_name) != set(operations):
                raise ValueError("handler operation/schema registry diverged")
            for name, operation in operations.items():
                tools.append(
                    GatewayTool(
                        name,
                        domain,
                        operation,
                        operation_risk(operation),
                        required_permission(operation),
                        by_name[name]["inputSchema"],
                    )
                )
        return cls(tuple(tools))
