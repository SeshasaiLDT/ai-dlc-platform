"""Transport-neutral local catalog; deliberately not enterprise MCP."""

from collections.abc import Mapping
from types import MappingProxyType

from .models import REQUEST_TYPES, LocalWorkspaceOperation, TrustedWorkspaceContext
from .service import GovernedWorkspaceService

LOCAL_WORKSPACE_TOOLS: Mapping[str, LocalWorkspaceOperation] = MappingProxyType(
    {f"workspace_{operation.value}": operation for operation in LocalWorkspaceOperation}
)


class LocalWorkspaceToolHandler:
    def __init__(self, service: GovernedWorkspaceService) -> None:
        self._service = service

    @staticmethod
    def definitions() -> tuple[dict[str, object], ...]:
        return tuple(
            {"name": name, "inputSchema": REQUEST_TYPES[operation].model_json_schema()}
            for name, operation in LOCAL_WORKSPACE_TOOLS.items()
        )

    def invoke(
        self, tool_name: str, arguments: Mapping[str, object], *, context: TrustedWorkspaceContext
    ) -> dict[str, object]:
        operation = LOCAL_WORKSPACE_TOOLS[tool_name]
        return self._service.invoke(operation, arguments, context=context).model_dump(
            mode="json", exclude_none=True
        )
