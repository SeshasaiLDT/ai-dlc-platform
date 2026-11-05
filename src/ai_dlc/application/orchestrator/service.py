"""Authorize a single capability request and delegate through the existing A2A client."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from a2a.types import AgentCard, AgentInterface
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT
from jsonschema import Draft202012Validator
from pydantic import Field

from ai_dlc.application.agent_harness import (
    A2AClient,
    ConfiguredAgentDirectory,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    create_agent_context,
)
from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.application.agent_registry import AgentRegistryReader
from ai_dlc.application.authorization import (
    AuthorizationRequest,
    AuthorizationService,
    CapabilityAction,
)
from ai_dlc.application.gateway.context import (
    AuthenticatedRuntimeSelection,
    GatewayContextResolver,
)
from ai_dlc.domain.identity import Capability, Principal


class SingleAgentRequest(ContractModel):
    initiative_id: str = Field(min_length=1)
    capability: Capability
    invocation: Invocation


class Orchestrator:
    def __init__(
        self,
        registry: AgentRegistryReader,
        contexts: GatewayContextResolver,
        authorization: AuthorizationService,
        transport_factory: Callable,
        *,
        timeout_seconds: float = 30,
        clock: Callable = lambda: datetime.now(UTC),
    ):
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("timeout must be in (0, 3600]")
        self.registry = registry
        self.contexts = contexts
        self.authorization = authorization
        self.transport_factory = transport_factory
        self.timeout = timeout_seconds
        self.clock = clock

    async def execute(self, request: SingleAgentRequest, principal: Principal) -> ExecutionResult:
        request_id, trace_id = uuid4().hex, uuid4().hex

        def failure(code, status=ExecutionStatus.FAILED):
            return ExecutionResult(
                status=status,
                request_id=request_id,
                correlation_id=request_id,
                trace_id=trace_id,
                error=ExecutionError(code=code, message=code),
            )

        try:
            trusted = self.contexts.resolve(
                AuthenticatedRuntimeSelection(
                    principal=principal,
                    initiative_id=request.initiative_id,
                    environment="runtime",
                    correlation_id=request_id,
                )
            )
            self.authorization.require(
                AuthorizationRequest(
                    principal, request.initiative_id, CapabilityAction(request.capability)
                ),
                profile=trusted.resolution.profile,
                initiative_revision=trusted.initiative_revision,
            )
        except Exception:
            return failure("authorization_failed")
        context = create_agent_context(
            task_id=uuid4().hex,
            authorization=trusted.resolution.authorization,
            request_id=request_id,
            correlation_id=request_id,
            trace_id=trace_id,
            deadline=self.clock() + timedelta(seconds=self.timeout),
        )
        try:
            candidates = self.registry.discover(
                required_capabilities=frozenset({request.capability.value}),
                allowed_permissions=frozenset(
                    permission.value for permission in context.authorization.tool_permissions
                ),
            )
            if not candidates:
                return failure("agent_not_found")
            if len(candidates) != 1:
                return failure("ambiguous_agent")
            manifest = candidates[0].manifest
            # Discovery metadata is not authority; independently enforce its requirements.
            if not candidates[0].enabled or manifest.health.status == "unhealthy":
                return failure("agent_not_found")
            if (
                not manifest.permissions
                <= {p.value for p in context.authorization.tool_permissions}
                or request.capability.value not in manifest.capabilities
            ):
                return failure("authorization_failed")
            if not Draft202012Validator(manifest.input_schema).is_valid(request.invocation.input):
                return failure("invalid_request")
            card = AgentCard(
                name=manifest.name,
                description=manifest.description,
                version=manifest.agent_version,
                supported_interfaces=[
                    AgentInterface(
                        url=manifest.a2a_endpoint,
                        protocol_binding="JSONRPC",
                        protocol_version=PROTOCOL_VERSION_CURRENT,
                    )
                ],
                default_input_modes=["application/json"],
                default_output_modes=["application/json"],
            )
            directory = ConfiguredAgentDirectory({manifest.agent_id: card}, lambda *_: True)
            client = self.transport_factory(directory)
            async with asyncio.timeout(self.timeout):
                result = await client.delegate(
                    manifest.agent_id, request.invocation, context=context
                )
            if (result.request_id, result.correlation_id, result.trace_id) != (
                context.request_id,
                context.correlation_id,
                context.trace_id,
            ):
                return failure("protocol_error")
            return result
        except TimeoutError:
            return failure("deadline_exceeded", ExecutionStatus.TIMED_OUT)
        except Exception:
            return failure("remote_unavailable")


def a2a_transport(*, call_context_factory, client_factory=None):
    """Bind trusted outbound credentials without putting them in message data."""
    return lambda directory: A2AClient(
        directory, call_context_factory=call_context_factory, client_factory=client_factory
    )
