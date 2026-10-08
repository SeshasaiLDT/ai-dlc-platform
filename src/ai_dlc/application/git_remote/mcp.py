"""Only remote Git operations; MCP runtime wiring belongs to AIDLC-36."""

from collections.abc import Mapping
from types import MappingProxyType

from ai_dlc.application.tool_policy import GitOperation

from .models import REQUEST_TYPES, TrustedGitContext
from .service import GovernedRemoteGitService

GIT_REMOTE_MCP_TOOLS: Mapping[str, GitOperation] = MappingProxyType(
    {
        "git_remote_get_repository": GitOperation.READ_REPOSITORY,
        "git_remote_get_branch": GitOperation.READ_BRANCH,
        "git_remote_list_branches": GitOperation.LIST_BRANCHES,
        "git_remote_get_diff": GitOperation.READ_DIFF,
        "git_remote_get_pull_request": GitOperation.READ_PR,
        "git_remote_create_branch": GitOperation.CREATE_BRANCH,
        "git_remote_update_branch": GitOperation.PUSH,
        "git_remote_create_pull_request": GitOperation.CREATE_PR,
        "git_remote_update_pull_request": GitOperation.UPDATE_PR,
    }
)


class GitRemoteMcpToolHandler:
    def __init__(self, service: GovernedRemoteGitService) -> None:
        self._service = service

    @staticmethod
    def definitions() -> tuple[dict[str, object], ...]:
        return tuple(
            {"name": name, "inputSchema": REQUEST_TYPES[operation].model_json_schema()}
            for name, operation in GIT_REMOTE_MCP_TOOLS.items()
        )

    def invoke(
        self, tool_name: str, arguments: Mapping[str, object], *, context: TrustedGitContext
    ) -> dict[str, object]:
        operation = GIT_REMOTE_MCP_TOOLS[tool_name]
        return self._service.invoke(operation, arguments, context=context).model_dump(
            mode="json", exclude_none=True
        )
