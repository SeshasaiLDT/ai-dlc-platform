"""Thin MCP-facing operation catalog; runtime SDK wiring belongs to AIDLC-36."""

from collections.abc import Mapping
from types import MappingProxyType

from ai_dlc.application.tool_policy import JiraOperation

from .models import REQUEST_TYPES, JiraToolResult, TrustedJiraContext
from .service import GovernedJiraService

JIRA_MCP_TOOLS: Mapping[str, JiraOperation] = MappingProxyType(
    {
        "jira_get_issue": JiraOperation.READ_ISSUE,
        "jira_search_issues": JiraOperation.SEARCH,
        "jira_create_issue": JiraOperation.CREATE_ISSUE,
        "jira_update_issue": JiraOperation.UPDATE_ISSUE,
        "jira_transition_issue": JiraOperation.TRANSITION_ISSUE,
        "jira_add_comment": JiraOperation.ADD_COMMENT,
    }
)


class JiraMcpToolHandler:
    def __init__(self, service: GovernedJiraService) -> None:
        self._service = service

    @staticmethod
    def definitions() -> tuple[dict[str, object], ...]:
        """Transport-neutral MCP names and public input schemas for runtime registration."""
        return tuple(
            {"name": name, "inputSchema": REQUEST_TYPES[operation].model_json_schema()}
            for name, operation in JIRA_MCP_TOOLS.items()
        )

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        context: TrustedJiraContext,
    ) -> dict[str, object]:
        operation = JIRA_MCP_TOOLS[tool_name]
        result: JiraToolResult = self._service.invoke(operation, arguments, context=context)
        return result.model_dump(mode="json", exclude_none=True)
