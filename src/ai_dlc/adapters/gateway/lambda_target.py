"""AgentCore Lambda target adapter; consume a one-use trusted runtime reference."""

from collections.abc import Mapping

from ai_dlc.application.gateway import GatewayRouter, TrustedGatewayContext
from ai_dlc.application.gateway.invocation import INVOCATION_REF_FIELD, TrustedInvocationRegistry
from ai_dlc.application.gateway.router import UnknownGatewayToolError
from ai_dlc.application.git_remote import GIT_REMOTE_MCP_TOOLS
from ai_dlc.application.jira import JIRA_MCP_TOOLS
from ai_dlc.application.servicenow import SERVICENOW_MCP_TOOLS


class GatewayLambdaTarget:
    """AWS metadata identifies routing; the opaque reference identifies the session."""

    def __init__(
        self, router: GatewayRouter, lookup: TrustedInvocationRegistry, *, target_name: str
    ) -> None:
        self._router = router
        self._lookup = lookup
        if target_name not in {"Jira", "ServiceNow", "RemoteGit"}:
            raise ValueError("unknown enterprise target")
        self._target_name = target_name
        self._allowed_names = {
            "Jira": JIRA_MCP_TOOLS,
            "ServiceNow": SERVICENOW_MCP_TOOLS,
            "RemoteGit": GIT_REMOTE_MCP_TOOLS,
        }[target_name]

    def invoke(self, event: Mapping[str, object], aws_context: object) -> dict[str, object]:
        if not isinstance(event, Mapping):
            raise TypeError("tool event must be an object")
        metadata = getattr(getattr(aws_context, "client_context", None), "custom", None)
        if not isinstance(metadata, Mapping):
            raise PermissionError("AgentCore invocation metadata required")
        required = (
            "bedrockAgentCoreMessageVersion",
            "bedrockAgentCoreGatewayId",
            "bedrockAgentCoreTargetId",
            "bedrockAgentCoreAwsRequestId",
            "bedrockAgentCoreMcpMessageId",
            "bedrockAgentCoreToolName",
        )
        if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in required):
            raise PermissionError("incomplete AgentCore invocation metadata")
        if metadata["bedrockAgentCoreMessageVersion"] != "1.0":
            raise PermissionError("unsupported AgentCore message version")
        name = metadata["bedrockAgentCoreToolName"]
        if not name.startswith(f"{self._target_name}___"):
            raise UnknownGatewayToolError("invalid AgentCore tool name")
        tool_name = name.split("___", 1)[1]
        if tool_name not in self._allowed_names:
            raise UnknownGatewayToolError("tool is not registered on this target")
        reference = event.get(INVOCATION_REF_FIELD)
        if not isinstance(reference, str):
            raise PermissionError("trusted invocation reference required")
        business_arguments = {
            key: value for key, value in event.items() if key != INVOCATION_REF_FIELD
        }
        context = self._lookup.resolve(
            reference,
            gateway_id=metadata["bedrockAgentCoreGatewayId"],
            target_id=metadata["bedrockAgentCoreTargetId"],
            tool_name=tool_name,
            message_id=metadata["bedrockAgentCoreMcpMessageId"],
            arguments=business_arguments,
        )
        if not isinstance(context, TrustedGatewayContext):
            raise PermissionError("trusted runtime session missing")
        return self._router.invoke(tool_name, business_arguments, context=context)
