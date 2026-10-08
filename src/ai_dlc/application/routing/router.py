"""Deterministic capability routing. Makes no model, classifier or A2A calls.

Precedence: (1) request validation, (2) explicit capability selection, (3) configured workflow,
(4) unresolved. A denied, invalid or misconfigured outcome is final and never falls through.
"""

from __future__ import annotations

from ai_dlc.application.agent_harness import AgentContext, TelemetryProvider
from ai_dlc.application.authorization import (
    AuthorizationRequest,
    AuthorizationService,
    CapabilityAction,
)
from ai_dlc.application.authorization.decisions import AuthorizationReason
from ai_dlc.domain.identity import Capability
from ai_dlc.domain.initiative import InitiativeProfile
from ai_dlc.domain.initiative.models import WorkflowRoute

from .models import (
    RoutingDecision,
    RoutingOutcome,
    RoutingReason,
    RoutingRequest,
    RoutingRule,
)

_EVENTS = {
    (RoutingOutcome.ROUTED, RoutingRule.EXPLICIT_CAPABILITY): "routing.explicit_selected",
    (RoutingOutcome.ROUTED, RoutingRule.CONFIGURED_WORKFLOW): "routing.workflow_matched",
}
_OUTCOME_EVENTS = {
    RoutingOutcome.UNRESOLVED: "routing.unresolved",
    RoutingOutcome.DENIED: "routing.denied",
    RoutingOutcome.CONFIGURATION_ERROR: "routing.configuration_error",
    RoutingOutcome.INVALID_REQUEST: "routing.invalid_request",
}


class DeterministicRouter:
    def __init__(
        self, authorizer: AuthorizationService, telemetry: TelemetryProvider | None = None
    ) -> None:
        self._authorizer = authorizer
        self._telemetry = telemetry

    def route(
        self,
        request: RoutingRequest,
        *,
        context: AgentContext,
        profile: InitiativeProfile,
        initiative_revision: int,
    ) -> RoutingDecision:
        """Route using the trusted ``context`` and the profile at ``initiative_revision``.

        The context is read, never modified. Identity and initiative are taken only from it.
        """
        if not isinstance(request, RoutingRequest):
            raise TypeError("request must be a RoutingRequest")
        if type(initiative_revision) is not int or initiative_revision < 1:
            raise ValueError("initiative_revision must be a positive integer")
        decision = self._decide(request, context, profile, initiative_revision)
        self._emit(decision, context)
        return decision

    def _decide(
        self,
        request: RoutingRequest,
        context: AgentContext,
        profile: InitiativeProfile,
        revision: int,
    ) -> RoutingDecision:
        def make(
            outcome: RoutingOutcome,
            rule: RoutingRule,
            reason: RoutingReason,
            capability: Capability | None = None,
            workflow_id: str | None = None,
        ) -> RoutingDecision:
            return RoutingDecision(
                outcome=outcome,
                rule=rule,
                reason=reason,
                initiative_id=context.authorization.initiative_id,
                initiative_revision=revision,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                trace_id=context.trace_id,
                task_id=context.task_id,
                capability=capability,
                workflow_id=workflow_id,
            )

        if request.malformed():
            return make(
                RoutingOutcome.INVALID_REQUEST, RoutingRule.NONE, RoutingReason.MALFORMED_REQUEST
            )
        if request.empty():
            return make(
                RoutingOutcome.INVALID_REQUEST, RoutingRule.NONE, RoutingReason.EMPTY_REQUEST
            )

        workflows = profile.routing.workflows
        locked_conflict: WorkflowRoute | None = None

        if request.explicit_capability is not None:
            rule = RoutingRule.EXPLICIT_CAPABILITY
            try:
                capability = Capability(request.explicit_capability)
            except ValueError:
                return make(RoutingOutcome.INVALID_REQUEST, rule, RoutingReason.UNKNOWN_CAPABILITY)
            denied = self._authorization_failure(capability, context, profile, revision)
            if denied:
                return make(
                    RoutingOutcome.DENIED, rule, RoutingReason.CAPABILITY_NOT_AUTHORIZED, None
                )
            failure = self._capability_failure(capability, profile)
            if failure is not None:
                outcome, reason = failure
                return make(outcome, rule, reason)
            for item in self._requested_workflows(request, workflows):
                if item.enabled and item.capability is not capability and item.capability_locked:
                    locked_conflict = item
            if locked_conflict is not None:
                return make(
                    RoutingOutcome.INVALID_REQUEST,
                    rule,
                    RoutingReason.WORKFLOW_CAPABILITY_CONFLICT,
                )
            return make(
                RoutingOutcome.ROUTED, rule, RoutingReason.EXPLICIT_CAPABILITY_SELECTED, capability
            )

        rule = RoutingRule.CONFIGURED_WORKFLOW
        if request.workflow_ids:
            matched: dict[str, list[WorkflowRoute]] = {
                wid: [w for w in workflows if w.id == wid] for wid in request.workflow_ids
            }
            if any(len(items) > 1 for items in matched.values()):
                return make(
                    RoutingOutcome.CONFIGURATION_ERROR,
                    rule,
                    RoutingReason.DUPLICATE_WORKFLOW_MAPPING,
                )
            known = [items[0] for items in matched.values() if items]
            if not known:
                return make(
                    RoutingOutcome.UNRESOLVED, RoutingRule.NONE, RoutingReason.UNKNOWN_WORKFLOW
                )
            enabled = [item for item in known if item.enabled]
            if not enabled:
                return make(
                    RoutingOutcome.UNRESOLVED, RoutingRule.NONE, RoutingReason.WORKFLOW_DISABLED
                )
            if len({item.capability for item in enabled}) > 1:
                return make(
                    RoutingOutcome.UNRESOLVED, RoutingRule.NONE, RoutingReason.AMBIGUOUS_WORKFLOW
                )
            chosen = enabled[0]
            capability = chosen.capability
            if self._authorization_failure(capability, context, profile, revision):
                return make(RoutingOutcome.DENIED, rule, RoutingReason.CAPABILITY_NOT_AUTHORIZED)
            if self._capability_failure(capability, profile) is not None:
                return make(
                    RoutingOutcome.CONFIGURATION_ERROR,
                    rule,
                    RoutingReason.WORKFLOW_CAPABILITY_UNAVAILABLE,
                )
            return make(
                RoutingOutcome.ROUTED,
                rule,
                RoutingReason.WORKFLOW_MATCHED,
                capability,
                chosen.id,
            )

        return make(
            RoutingOutcome.UNRESOLVED, RoutingRule.NONE, RoutingReason.NO_DETERMINISTIC_MATCH
        )

    @staticmethod
    def _requested_workflows(
        request: RoutingRequest, workflows: tuple[WorkflowRoute, ...]
    ) -> list[WorkflowRoute]:
        wanted = set(request.workflow_ids)
        return [item for item in workflows if item.id in wanted]

    def _authorization_failure(
        self,
        capability: Capability,
        context: AgentContext,
        profile: InitiativeProfile,
        revision: int,
    ) -> bool:
        """Delegate to the existing AuthorizationService (which audits). True when not allowed."""
        auth = context.authorization
        decision = self._authorizer.evaluate(
            AuthorizationRequest(auth.principal, auth.initiative_id, CapabilityAction(capability)),
            profile=profile,
            initiative_revision=revision,
        )
        return decision.reason is not AuthorizationReason.ALLOWED

    @staticmethod
    def _capability_failure(
        capability: Capability, profile: InitiativeProfile
    ) -> tuple[RoutingOutcome, RoutingReason] | None:
        configured = [r for r in profile.routing.capabilities if r.capability is capability]
        if not configured:
            return RoutingOutcome.CONFIGURATION_ERROR, RoutingReason.CAPABILITY_NOT_CONFIGURED
        if len(configured) > 1:
            return RoutingOutcome.CONFIGURATION_ERROR, RoutingReason.CAPABILITY_NOT_CONFIGURED
        if not configured[0].enabled:
            return RoutingOutcome.CONFIGURATION_ERROR, RoutingReason.CAPABILITY_DISABLED
        return None

    def _emit(self, decision: RoutingDecision, context: AgentContext) -> None:
        """Best effort and metadata-free: names and reason codes only. Never alters the result."""
        if self._telemetry is None:
            return
        name = _EVENTS.get((decision.outcome, decision.rule)) or _OUTCOME_EVENTS[decision.outcome]
        try:
            self._telemetry.record_event(name, context=context)
            self._telemetry.record_metric(
                f"routing.reason.{decision.reason.value}", 1.0, context=context
            )
        except Exception:
            return
