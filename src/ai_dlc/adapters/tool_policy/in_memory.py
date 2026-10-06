"""Deterministic in-memory policy and audit adapters."""

from threading import Lock

from ai_dlc.application.tool_policy.models import ToolPolicy, ToolPolicyAuditEvent
from ai_dlc.application.tool_policy.operations import ToolKind


class InMemoryToolPolicyRepository:
    def __init__(self, policies: tuple[ToolPolicy, ...] = ()) -> None:
        values = tuple(policies)
        if any(not isinstance(policy, ToolPolicy) for policy in values):
            raise TypeError("policies must contain ToolPolicy values")
        self._policies = values

    def policies_for(self, initiative_id: str, tool: ToolKind) -> tuple[ToolPolicy, ...]:
        return tuple(
            policy
            for policy in self._policies
            if policy.initiative_id == initiative_id and policy.tool is tool
        )


class InMemoryToolPolicyAuditSink:
    def __init__(self) -> None:
        self._events: list[ToolPolicyAuditEvent] = []
        self._lock = Lock()

    def record(self, event: ToolPolicyAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[ToolPolicyAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
