"""Authorize, bound and durably accept plans. No dispatch or expansion occurs here."""

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from ai_dlc.application.authorization import AuthorizationRequest, CapabilityAction
from ai_dlc.application.gateway.context import AuthenticatedRuntimeSelection
from ai_dlc.domain.identity import Principal

from .models import PlanLimits, PlanningRequest, TaskPlan, order_steps


class PlanError(Exception):
    """Safe application error with a stable code."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class PlanRepository(Protocol):
    def accept(self, plan: TaskPlan) -> TaskPlan:
        """Atomic insert; identical owner/request submissions return original, others conflict."""
        ...

    def get(self, plan_id: str) -> TaskPlan:
        """Load and revalidate an accepted plan or raise PlanError."""
        ...


class PlanningService:
    def __init__(
        self,
        registry,
        contexts,
        authorization,
        repository: PlanRepository,
        *,
        limits=None,
        clock=lambda: datetime.now(UTC),
    ):
        self.registry = registry
        self.contexts = contexts
        self.authorization = authorization
        self.repository = repository
        self.limits = limits if limits is not None else PlanLimits()
        self.clock = clock

    def _authorize(self, request, principal):
        try:
            trusted = self.contexts.resolve(
                AuthenticatedRuntimeSelection(
                    principal,
                    request.initiative_id,
                    "runtime",
                    request.correlation_id,
                )
            )
            for capability in sorted({s.capability for s in request.steps}):
                self.authorization.require(
                    AuthorizationRequest(
                        principal, request.initiative_id, CapabilityAction(capability)
                    ),
                    profile=trusted.resolution.profile,
                    initiative_revision=trusted.initiative_revision,
                )
            return trusted.resolution.authorization
        except Exception:
            raise PlanError("authorization_failed") from None

    def accept(self, request: PlanningRequest, principal: Principal) -> TaskPlan:
        # Revalidate at the boundary, including mutable nested JSON payloads.
        request = PlanningRequest.model_validate_json(request.model_dump_json())
        steps = order_steps(request.steps, self.limits)
        authorization = self._authorize(request, principal)
        for step in steps:
            if not self.registry.discover(
                required_capabilities=frozenset({step.capability.value}),
                allowed_permissions=frozenset(p.value for p in authorization.tool_permissions),
            ):
                raise PlanError("agent_not_found")
        plan = TaskPlan(
            **request.model_dump(exclude={"steps"}),
            steps=steps,
            plan_id=uuid4().hex,
            principal_id=principal.subject_id,
            identity_provider=principal.provider,
            created_at=self.clock(),
            limits=self.limits,
        )
        return self.repository.accept(plan)

    def load_for_execution(
        self, plan_id: str, initiative_id: str, principal: Principal
    ) -> TaskPlan:
        """AIDLC-56 admission: accepts an ID, never an unsaved plan or permission snapshot.

        This is not execution or a permission lease. Dispatch must reselect agents and enforce
        operation policies and these stored leaf/delegation budgets.
        """
        if not isinstance(plan_id, str):
            raise PlanError("plan_not_found")
        plan = self.repository.get(plan_id)
        if (plan.initiative_id, plan.principal_id, plan.identity_provider) != (
            initiative_id,
            principal.subject_id,
            principal.provider,
        ):
            raise PlanError("authorization_failed")
        order_steps(plan.steps, self.limits)
        self._authorize(plan, principal)
        return plan
