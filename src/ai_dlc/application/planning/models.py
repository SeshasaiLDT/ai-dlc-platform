"""Version 1 bounded capability graphs. Step inputs are data, never recursive plans."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ai_dlc.application.agent_harness.models import ContractModel, Invocation
from ai_dlc.application.agent_harness.structured_output import _safe_schema
from ai_dlc.domain.identity import Capability

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class PlanLimits(ContractModel):
    max_steps: int = Field(default=32, ge=1, le=64, strict=True)
    max_depth: int = Field(default=8, ge=1, le=16, strict=True)
    max_delegations: int = Field(default=32, ge=1, le=64, strict=True)
    # Every step is a leaf; a future executor must not expand or replan a step.
    nested_delegation: Literal[False] = False


class PlanStep(ContractModel):
    step_id: Identifier
    capability: Capability
    dependencies: tuple[Identifier, ...] = ()
    invocation: Invocation
    # Target input key -> direct dependency's complete output. No implicit JSON pointers.
    upstream_outputs: dict[str, Identifier] = Field(default_factory=dict)
    expected_output: dict = Field(min_length=1)

    @field_validator("expected_output")
    @classmethod
    def schema(cls, value):
        return _safe_schema(value)

    @model_validator(mode="after")
    def references(self):
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("duplicate dependencies")
        if self.step_id in self.dependencies:
            raise ValueError("self dependency")
        if not set(self.upstream_outputs.values()) <= set(self.dependencies):
            raise ValueError("output references must name direct dependencies")
        if any(not key.strip() for key in self.upstream_outputs):
            raise ValueError("input names must be nonblank")
        if set(self.upstream_outputs) & set(self.invocation.input):
            raise ValueError("literal and upstream inputs overlap")
        return self


class PlanningRequest(ContractModel):
    initiative_id: Identifier
    request_id: Identifier
    correlation_id: Identifier
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def graph(self):
        order_steps(self.steps, PlanLimits(max_steps=64, max_depth=16, max_delegations=64))
        if len(self.model_dump_json()) > 262_144:
            raise ValueError("plan request exceeds 256 KiB")
        return self


def order_steps(steps: tuple[PlanStep, ...], limits: PlanLimits) -> tuple[PlanStep, ...]:
    if not steps or len(steps) > min(limits.max_steps, limits.max_delegations):
        raise ValueError("plan size/delegation budget exceeded")
    by_id = {step.step_id: step for step in steps}
    if len(by_id) != len(steps):
        raise ValueError("duplicate step IDs")
    if any(set(step.dependencies) - by_id.keys() for step in steps):
        raise ValueError("missing dependency")
    ordered, depths = [], {}
    while len(ordered) < len(steps):
        ready = sorted(
            name
            for name, step in by_id.items()
            if name not in depths and set(step.dependencies) <= depths.keys()
        )
        if not ready:
            raise ValueError("cyclic plan")
        for name in ready:
            step = by_id[name]
            depth = 1 + max((depths[dep] for dep in step.dependencies), default=0)
            if depth > limits.max_depth:
                raise ValueError("dependency depth exceeded")
            depths[name] = depth
            ordered.append(step)
    return tuple(ordered)


class TaskPlan(PlanningRequest):
    schema_version: Literal["1.0.0"] = "1.0.0"
    plan_id: Identifier
    principal_id: str = Field(min_length=1)
    identity_provider: str = Field(min_length=1)
    created_at: datetime
    limits: PlanLimits
    state: Literal["accepted"] = "accepted"

    @model_validator(mode="after")
    def bounded(self):
        if self.created_at.tzinfo is None:
            raise ValueError("timezone-aware creation time required")
        if self.steps != order_steps(self.steps, self.limits):
            raise ValueError("plan steps must be in canonical dependency order")
        return self


def submission(plan: TaskPlan) -> str:
    """Stable idempotency content; creation time and generated identity are excluded."""
    data = plan.model_dump(mode="json", exclude={"created_at", "plan_id"})
    return json.dumps(data, sort_keys=True, allow_nan=False)
