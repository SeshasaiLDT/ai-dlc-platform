"""Compatibility re-export; the contracts live in ``ai_dlc.domain.authorization`` so the
Agent Harness SDK can depend on them without the authorization services."""

from ai_dlc.domain.authorization import (
    LogicalScopes,
    ResolvedAuthorizationContext,
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
)

__all__ = [
    "LogicalScopes",
    "ResolvedAuthorizationContext",
    "RoleGrant",
    "RolePolicy",
    "ScopeRestriction",
]
