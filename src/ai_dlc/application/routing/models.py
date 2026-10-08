"""Routing request and decision contracts for deterministic *capability* routing.

Capability routing (which AI-DLC capability handles a request) is distinct from model routing
(which LLM deployment serves a model role). Nothing here selects or invokes a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from ai_dlc.domain.identity import Capability

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_WORKFLOW_IDS = 8
MAX_CONTENT_CHARS = 100_000


class RoutingOutcome(StrEnum):
    ROUTED = "routed"
    UNRESOLVED = "unresolved"
    DENIED = "denied"
    INVALID_REQUEST = "invalid_request"
    CONFIGURATION_ERROR = "configuration_error"


class RoutingRule(StrEnum):
    EXPLICIT_CAPABILITY = "explicit_capability"
    CONFIGURED_WORKFLOW = "configured_workflow"
    MODEL_SUGGESTED = "model_suggested"  # untrusted suggestion that passed the same checks
    NONE = "none"


class RoutingReason(StrEnum):
    EXPLICIT_CAPABILITY_SELECTED = "explicit_capability_selected"
    WORKFLOW_MATCHED = "workflow_matched"
    MODEL_SUGGESTION_ACCEPTED = "model_suggestion_accepted"
    CAPABILITY_NOT_AUTHORIZED = "capability_not_authorized"
    UNKNOWN_CAPABILITY = "unknown_capability"
    CAPABILITY_NOT_CONFIGURED = "capability_not_configured"
    CAPABILITY_DISABLED = "capability_disabled"
    WORKFLOW_CAPABILITY_CONFLICT = "workflow_capability_conflict"
    WORKFLOW_CAPABILITY_UNAVAILABLE = "workflow_capability_unavailable"
    DUPLICATE_WORKFLOW_MAPPING = "duplicate_workflow_mapping"
    MALFORMED_REQUEST = "malformed_request"
    EMPTY_REQUEST = "empty_request"
    UNKNOWN_WORKFLOW = "unknown_workflow"
    WORKFLOW_DISABLED = "workflow_disabled"
    AMBIGUOUS_WORKFLOW = "ambiguous_workflow"
    NO_DETERMINISTIC_MATCH = "no_deterministic_match"


# Only genuine lack of a deterministic match or genuine ambiguity may reach a classifier.
CLASSIFIER_ELIGIBLE_REASONS = frozenset(
    {
        RoutingReason.UNKNOWN_WORKFLOW,
        RoutingReason.AMBIGUOUS_WORKFLOW,
        RoutingReason.NO_DETERMINISTIC_MATCH,
    }
)


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """Untrusted caller input. Identity, roles and initiative come from the trusted context.

    ``explicit_capability`` is a user's selection (not a model hint). ``content`` is untrusted
    free text: it is never used for matching, never logged, and never copied into a decision.
    """

    explicit_capability: str | None = None
    workflow_ids: tuple[str, ...] = ()
    content: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.explicit_capability is not None and not isinstance(self.explicit_capability, str):
            raise TypeError("explicit_capability must be a string")
        if not isinstance(self.workflow_ids, tuple) or any(
            not isinstance(item, str) for item in self.workflow_ids
        ):
            raise TypeError("workflow_ids must be a tuple of strings")
        if self.content is not None and not isinstance(self.content, str):
            raise TypeError("content must be a string")

    def malformed(self) -> bool:
        """Structural problems that make the request invalid (never guessed around)."""
        if self.explicit_capability is not None and not _IDENTIFIER.match(self.explicit_capability):
            return True
        if len(self.workflow_ids) > MAX_WORKFLOW_IDS or len(set(self.workflow_ids)) != len(
            self.workflow_ids
        ):
            return True
        if any(not _IDENTIFIER.match(item) for item in self.workflow_ids):
            return True
        return self.content is not None and len(self.content) > MAX_CONTENT_CHARS

    def empty(self) -> bool:
        return (
            self.explicit_capability is None
            and not self.workflow_ids
            and not (self.content and self.content.strip())
        )


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Safe, structured result. A capability is logical; there are no endpoints or ARNs."""

    outcome: RoutingOutcome
    rule: RoutingRule
    reason: RoutingReason
    initiative_id: str
    initiative_revision: int
    request_id: str
    correlation_id: str
    trace_id: str
    task_id: str | None = None
    capability: Capability | None = None
    workflow_id: str | None = None

    def __post_init__(self) -> None:
        routed = self.outcome is RoutingOutcome.ROUTED
        if routed != (self.capability is not None):
            raise ValueError("a capability is present exactly when the outcome is routed")
        if routed and self.rule is RoutingRule.NONE:
            raise ValueError("a routed decision names its rule")
        if self.outcome is RoutingOutcome.UNRESOLVED and self.reason not in (
            CLASSIFIER_ELIGIBLE_REASONS | {RoutingReason.WORKFLOW_DISABLED}
        ):
            raise ValueError("invalid unresolved reason")

    @property
    def classifier_candidate(self) -> bool:
        """True only for unresolved requests with no deterministic match or real ambiguity.

        Denied, invalid, misconfigured and administratively-disabled outcomes are never
        candidates, so classification can never be used to get around them.
        """
        return (
            self.outcome is RoutingOutcome.UNRESOLVED and self.reason in CLASSIFIER_ELIGIBLE_REASONS
        )
