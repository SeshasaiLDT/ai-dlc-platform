"""Authorize a single capability request and delegate through the existing A2A client."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
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
from ai_dlc.application.agent_harness.models import ContractModel, DelegationTrackingError
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


class DispatchLineage(ContractModel):
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


@dataclass(frozen=True)
class DispatchOutcome:
    result: ExecutionResult
    agent_id: str | None


DispatchTrackingError = DelegationTrackingError


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
        request_id = uuid4().hex
        return (
            await self.dispatch(
                request,
                principal,
                DispatchLineage(
                    request_id=request_id,
                    correlation_id=request_id,
                    trace_id=uuid4().hex,
                    task_id=uuid4().hex,
                    session_id=request_id,
                ),
            )
        ).result

    async def dispatch(
        self,
        request: SingleAgentRequest,
        principal: Principal,
        lineage: DispatchLineage,
        *,
        cancellation_event=None,
        on_progress=None,
        remote_task_id=None,
        cancel_remote=False,
        expected_agent=None,
        expected_endpoint=None,
        deadline=None,
    ) -> DispatchOutcome:
        """Shared trusted dispatch; lineage is supplied by application code, never wire grants."""
        request_id, trace_id = lineage.request_id, lineage.trace_id
        selected_agent = None

        def failure(code, status=ExecutionStatus.FAILED, *, metadata=None):
            return DispatchOutcome(
                ExecutionResult(
                    status=status,
                    request_id=request_id,
                    correlation_id=lineage.correlation_id,
                    trace_id=trace_id,
                    error=ExecutionError(code=code, message=code),
                    metadata=metadata,
                ),
                selected_agent,
            )

        try:
            trusted = self.contexts.resolve(
                AuthenticatedRuntimeSelection(
                    principal=principal,
                    initiative_id=request.initiative_id,
                    environment="runtime",
                    correlation_id=lineage.correlation_id,
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
            task_id=lineage.task_id,
            session_id=lineage.session_id,
            authorization=trusted.resolution.authorization,
            request_id=request_id,
            correlation_id=lineage.correlation_id,
            trace_id=trace_id,
            deadline=min(deadline, self.clock() + timedelta(seconds=self.timeout))
            if deadline is not None
            else self.clock() + timedelta(seconds=self.timeout),
            cancellation_event=cancellation_event,
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
            selected_agent = manifest.agent_id
            # Discovery metadata is not authority; independently enforce its requirements.
            if not candidates[0].enabled or manifest.health.status == "unhealthy":
                return failure("agent_not_found")
            if (
                not manifest.permissions
                <= {p.value for p in context.authorization.tool_permissions}
                or request.capability.value not in manifest.capabilities
            ):
                return failure("authorization_failed")
            if remote_task_id is not None and (
                manifest.agent_id != expected_agent or manifest.a2a_endpoint != expected_endpoint
            ):
                return failure("protocol_error")
            if remote_task_id is None and not Draft202012Validator(manifest.input_schema).is_valid(
                request.invocation.input
            ):
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

            def tracked(call_id, remote_id):
                try:
                    on_progress(call_id, remote_id, manifest.agent_id, manifest.a2a_endpoint)
                except Exception:
                    raise DispatchTrackingError from None

            async with asyncio.timeout(self.timeout):
                if remote_task_id is not None:
                    result = await client.control_task(
                        manifest.agent_id, remote_task_id, context=context, cancel=cancel_remote
                    )
                elif on_progress is not None:
                    result = await client.delegate(
                        manifest.agent_id, request.invocation, context=context, on_progress=tracked
                    )
                else:
                    result = await client.delegate(
                        manifest.agent_id, request.invocation, context=context
                    )
            if (result.request_id, result.correlation_id, result.trace_id) != (
                context.request_id,
                context.correlation_id,
                context.trace_id,
            ):
                return failure("protocol_error", metadata=result.metadata)
            if result.status is ExecutionStatus.SUCCEEDED and (
                result.output is None
                or not Draft202012Validator(manifest.output_schema).is_valid(result.output)
            ):
                return failure("invalid_output", metadata=result.metadata)
            return DispatchOutcome(result, selected_agent)
        except DispatchTrackingError:
            raise
        except TimeoutError:
            return failure("deadline_exceeded", ExecutionStatus.TIMED_OUT)
        except Exception:
            return failure("remote_unavailable")


def a2a_transport(*, call_context_factory, client_factory=None):
    """Bind trusted outbound credentials without putting them in message data."""
    return lambda directory: A2AClient(
        directory, call_context_factory=call_context_factory, client_factory=client_factory
    )
