"""Durable version 1 execution state, separate from immutable accepted plans."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.application.planning.models import Identifier
from ai_dlc.application.workflow.executor import StepResult


class RetryPolicy(ContractModel):
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)
    backoff_seconds: float = Field(default=2, gt=0, le=300, allow_inf_nan=False)
    execution_seconds: float = Field(default=3600, gt=0, le=86400, allow_inf_nan=False)
    # Trusted configuration; never accepted from the HTTP request.
    retry_safe_capabilities: frozenset[str] = frozenset()


class StartExecution(ContractModel):
    plan_id: Identifier
    initiative_id: Identifier
    idempotency_key: Identifier


class ExecutionRequest(ContractModel):
    execution_id: Identifier
    initiative_id: Identifier


class Attempt(ContractModel):
    number: int = Field(ge=1)
    started_at: datetime
    deadline: datetime
    finished_at: datetime | None = None
    call_id: str | None = None
    remote_task_id: str | None = None
    agent_id: str | None = None
    endpoint: str | None = None
    outcome: Literal["active", "succeeded", "failed", "uncertain", "cancelled"] = "active"
    retry_eligible: bool = False
    retry_at: datetime | None = None

    @model_validator(mode="after")
    def valid(self):
        for instant in (self.started_at, self.deadline, self.finished_at, self.retry_at):
            if instant is not None and instant.tzinfo is None:
                raise ValueError("aware attempt timestamps required")
        if self.retry_eligible and (self.retry_at is None or self.outcome != "failed"):
            raise ValueError("only finished failed attempts may be retry eligible")
        return self


class DurableStep(ContractModel):
    step_id: str
    task_id: str
    state: Literal[
        "queued", "active", "succeeded", "failed", "blocked", "uncertain", "cancelled"
    ] = "queued"
    attempts: tuple[Attempt, ...] = ()
    result: StepResult | None = None


class ExecutionRecord(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    execution_id: str
    plan_id: str
    initiative_id: str
    principal_id: str
    provider: str
    idempotency_key: str
    request_id: str
    correlation_id: str
    trace_id: str
    created_at: datetime
    updated_at: datetime
    deadline: datetime
    policy: RetryPolicy
    steps: tuple[DurableStep, ...]
    state: Literal[
        "queued", "active", "succeeded", "partial", "failed", "cancelled", "uncertain"
    ] = "queued"
    cancel_requested: bool = False
    cancellation: Literal[
        "none", "requested", "confirmed", "unsupported", "unknown", "completed"
    ] = "none"
    revision: int = Field(default=1, ge=1)
    claim_token: str | None = None
    lease_until: datetime | None = None

    @model_validator(mode="after")
    def valid(self):
        for instant in (self.created_at, self.updated_at, self.deadline, self.lease_until):
            if instant is not None and instant.tzinfo is None:
                raise ValueError("aware timestamps required")
        if len({s.step_id for s in self.steps}) != len(self.steps) or not self.steps:
            raise ValueError("unique steps required")
        for step in self.steps:
            if step.state in {"active", "uncertain"} and not step.attempts:
                raise ValueError("active/uncertain steps require durable attempt intent")
            if len(step.attempts) > self.policy.max_attempts:
                raise ValueError("attempt budget exceeded")
            if [a.number for a in step.attempts] != list(range(1, len(step.attempts) + 1)):
                raise ValueError("attempt order invalid")
            if step.state == "succeeded" and (
                step.result is None or step.result.status != "succeeded"
            ):
                raise ValueError("successful steps require persisted successful results")
        return self
