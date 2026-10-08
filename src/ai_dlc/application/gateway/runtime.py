"""Agent harness facade: discovery is filtered and enterprise calls use Gateway."""

from collections.abc import Mapping
from typing import Protocol
from uuid import uuid4

from .context import TrustedGatewayContext
from .discovery import GatewayDiscovery
from .invocation import INVOCATION_REF_FIELD, TrustedInvocationRegistry
from .router import UnknownGatewayToolError


class GatewayInvocationTransport(Protocol):
    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        context: TrustedGatewayContext,
        message_id: str,
    ) -> dict[str, object]:
        """Invoke the approved AgentCore Gateway using runtime-held credentials."""


class GatewayRuntimeAccess:
    def __init__(
        self,
        discovery: GatewayDiscovery,
        transport: GatewayInvocationTransport,
        invocations: TrustedInvocationRegistry,
    ) -> None:
        self._discovery = discovery
        self._transport = transport
        self._invocations = invocations

    def available_tools(
        self,
        *,
        context: TrustedGatewayContext,
    ) -> tuple[dict[str, object], ...]:
        return tuple(item.public_definition() for item in self._discovery.available_tools(context))

    def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        context: TrustedGatewayContext,
    ) -> dict[str, object]:
        if tool_name not in {item.tool.name for item in self._discovery.available_tools(context)}:
            raise UnknownGatewayToolError("tool is unavailable to this session")
        if INVOCATION_REF_FIELD in arguments:
            raise PermissionError("runtime invocation reference cannot be agent supplied")
        message_id = str(uuid4())
        issued = self._invocations.issue(
            tool_name, arguments, context=context, message_id=message_id
        )
        return self._transport.invoke(
            tool_name,
            {**arguments, INVOCATION_REF_FIELD: issued.reference},
            context=context,
            message_id=message_id,
        )
