"""Storage-neutral tool policy and audit boundaries."""

from typing import Protocol

from .models import ToolPolicy, ToolPolicyAuditEvent
from .operations import ToolKind


class ToolPolicyRepository(Protocol):
    def policies_for(self, initiative_id: str, tool: ToolKind) -> tuple[ToolPolicy, ...]:
        """Return immutable policies for one initiative and tool."""


class ToolPolicyAuditSink(Protocol):
    def record(self, event: ToolPolicyAuditEvent) -> None:
        """Record one immutable audit-safe policy decision."""
