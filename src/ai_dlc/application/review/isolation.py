"""Attenuated authorization for review execution.

The reviewer runs with a *derived* copy of the trusted context that has no write or admin tool
permissions, whatever the requesting user may hold elsewhere. The copy is derived only from the
trusted context, never from request fields, and the original is left untouched.

Limit: this removes write permissions from every platform component that checks the context
(resource bindings, gateway). It cannot by itself stop an external system from writing; production
must also enforce read-only credentials / branch protection / object-lock for the reviewed artifact.
"""

from __future__ import annotations

from dataclasses import replace

from ai_dlc.application.agent_harness import AgentContext
from ai_dlc.domain.identity import ToolPermission

WRITE_PERMISSIONS = frozenset(
    {
        ToolPermission.JIRA_WRITE,
        ToolPermission.GIT_WRITE,
        ToolPermission.SERVICENOW_WRITE,
        ToolPermission.ARTIFACT_WRITE,
    }
)


def attenuate_for_review(context: AgentContext) -> AgentContext:
    """Return a context for reviewer execution: read permissions only, no admin permissions."""
    if not isinstance(context, AgentContext):
        raise TypeError("trusted AgentContext required")
    auth = context.authorization
    reviewer_auth = replace(
        auth,
        tool_permissions=auth.tool_permissions - WRITE_PERMISSIONS,
        admin_permissions=frozenset(),
    )
    return replace(context, authorization=reviewer_auth)
