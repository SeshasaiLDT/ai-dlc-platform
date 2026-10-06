"""Membership lookup boundary; persistence technology is deliberately unspecified."""

from typing import Protocol

from ai_dlc.domain.identity import InitiativeMembership


class MembershipRepository(Protocol):
    def get_membership(self, principal_id: str, initiative_id: str) -> InitiativeMembership:
        """Return enabled membership; raise missing or disabled typed errors."""

    def list_memberships(self, principal_id: str) -> tuple[InitiativeMembership, ...]:
        """List all memberships, including disabled, sorted by initiative ID."""
