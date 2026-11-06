"""Deterministic leaf delegation with explicit failure and provenance envelopes."""

from copy import deepcopy
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from jsonschema import Draft202012Validator
from pydantic import Field, JsonValue

from ai_dlc.application.agent_harness import ExecutionError, ExecutionStatus, Invocation
from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.application.orchestrator.service import (
    DispatchLineage,
    Orchestrator,
    SingleAgentRequest,
)
from ai_dlc.application.planning import PlanningService, TaskPlan
from ai_dlc.application.planning.models import Identifier, order_steps
from ai_dlc.domain.identity import Capability, Principal


class ExecutePlanRequest(ContractModel):
    plan_id: Identifier
    initiative_id: Identifier


class StepResult(ContractModel):
    step_id: str
    task_id: str
    capability: Capability
    agent_id: str | None = None
    status: Literal["succeeded", "failed", "blocked"]
    output: dict[str, JsonValue] | None = None
    # Untrusted source/evidence metadata, retained verbatim on successful output.
    evidence_metadata: dict[str, JsonValue] | None = None
    remote_task_id: str | None = None
    call_id: str | None = None
    error: ExecutionError | None = None
    blocked_dependencies: tuple[str, ...] = ()
    upstream_sources: dict[str, str] = Field(default_factory=dict)


class WorkflowResult(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    plan_id: str
    execution_id: str
    initiative_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    outcome: Literal["succeeded", "partial_failure", "failed"]
    steps: tuple[StepResult, ...]


_SAFE_CODES = frozenset(
    {
        "authorization_failed",
        "agent_not_found",
        "ambiguous_agent",
        "invalid_request",
        "invalid_output",
        "protocol_error",
        "remote_unavailable",
        "deadline_exceeded",
        "remote_timeout",
        "remote_execution_failed",
        "remote_rejected",
        "remote_task_pending",
        "remote_cancelled",
        "cancelled",
        "authentication_failed",
        "unsupported_a2a_method",
    }
)


class WorkflowExecutor:
    def __init__(self, planning: PlanningService, orchestrator: Orchestrator):
        self.planning = planning
        self.orchestrator = orchestrator

    async def execute(self, request: ExecutePlanRequest, principal: Principal) -> WorkflowResult:
        plan = self.planning.load_for_execution(request.plan_id, request.initiative_id, principal)
        # Revalidate snapshots and enforce both stored and tighter current configuration.
        plan = TaskPlan.model_validate_json(plan.model_dump_json())
        order_steps(plan.steps, plan.limits)
        steps = order_steps(plan.steps, self.planning.limits)
        execution_id, trace_id = uuid4().hex, uuid4().hex
        results = {}
        for step in steps:
            task_id = uuid5(NAMESPACE_URL, f"{execution_id}/{step.step_id}").hex
            common = dict(
                step_id=step.step_id,
                task_id=task_id,
                capability=step.capability,
                upstream_sources=dict(step.upstream_outputs),
            )
            blocked = tuple(dep for dep in step.dependencies if results[dep].status != "succeeded")
            if blocked:
                results[step.step_id] = StepResult(
                    **common,
                    status="blocked",
                    blocked_dependencies=blocked,
                    error=ExecutionError(
                        code="dependency_failed", message="Required dependency failed"
                    ),
                )
                continue
            inputs = deepcopy(step.invocation.input)
            for key, dependency in step.upstream_outputs.items():
                if key in inputs:
                    raise ValueError("conflicting upstream binding")
                inputs[key] = deepcopy(results[dependency].output)
            outcome = await self.orchestrator.dispatch(
                SingleAgentRequest(
                    initiative_id=plan.initiative_id,
                    capability=step.capability,
                    invocation=Invocation(input=inputs, metadata=step.invocation.metadata),
                ),
                principal,
                DispatchLineage(
                    request_id=plan.request_id,
                    correlation_id=plan.correlation_id,
                    trace_id=trace_id,
                    task_id=task_id,
                    session_id=execution_id,
                ),
            )
            result = outcome.result
            error = result.error
            if result.status is ExecutionStatus.SUCCEEDED and not Draft202012Validator(
                step.expected_output
            ).is_valid(result.output):
                error = ExecutionError(code="invalid_output", message="Invalid step output")
            succeeded = result.status is ExecutionStatus.SUCCEEDED and error is None
            metadata = result.metadata or {}
            results[step.step_id] = StepResult(
                **common,
                agent_id=outcome.agent_id,
                status="succeeded" if succeeded else "failed",
                output=deepcopy(result.output) if succeeded else None,
                evidence_metadata=deepcopy(result.metadata) if succeeded else None,
                remote_task_id=metadata.get("remote_task_id")
                if isinstance(metadata.get("remote_task_id"), str)
                else None,
                call_id=metadata.get("delegation_message_id")
                if isinstance(metadata.get("delegation_message_id"), str)
                else None,
                error=None
                if succeeded
                else ExecutionError(
                    code=error.code
                    if error and error.code in _SAFE_CODES
                    else "remote_execution_failed",
                    message="Step delegation failed",
                ),
            )
        count = sum(result.status == "succeeded" for result in results.values())
        return WorkflowResult(
            plan_id=plan.plan_id,
            execution_id=execution_id,
            initiative_id=plan.initiative_id,
            request_id=plan.request_id,
            correlation_id=plan.correlation_id,
            trace_id=trace_id,
            outcome="succeeded"
            if count == len(steps)
            else "partial_failure"
            if count
            else "failed",
            steps=tuple(results.values()),
        )
