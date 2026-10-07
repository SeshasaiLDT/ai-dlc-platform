"""Transport-neutral ServiceNow MCP catalog; runtime registration belongs to AIDLC-36."""

from collections.abc import Mapping
from types import MappingProxyType

from ai_dlc.application.tool_policy import ServiceNowOperation

from .models import REQUEST_TYPES, TrustedServiceNowContext
from .service import GovernedServiceNowService

SERVICENOW_MCP_TOOLS: Mapping[str, ServiceNowOperation] = MappingProxyType(
    {
        "servicenow_get_record": ServiceNowOperation.READ_RECORD,
        "servicenow_search_records": ServiceNowOperation.SEARCH,
        "servicenow_create_record": ServiceNowOperation.CREATE_RECORD,
        "servicenow_update_record": ServiceNowOperation.UPDATE_RECORD,
        "servicenow_add_comment": ServiceNowOperation.ADD_COMMENT,
    }
)


class ServiceNowMcpToolHandler:
    def __init__(self, service: GovernedServiceNowService) -> None:
        self._service = service

    @staticmethod
    def definitions() -> tuple[dict[str, object], ...]:
        return tuple(
            {"name": name, "inputSchema": REQUEST_TYPES[operation].model_json_schema()}
            for name, operation in SERVICENOW_MCP_TOOLS.items()
        )

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        context: TrustedServiceNowContext,
    ) -> dict[str, object]:
        operation = SERVICENOW_MCP_TOOLS[tool_name]
        result = self._service.invoke(operation, arguments, context=context)
        return result.model_dump(mode="json", exclude_none=True)
