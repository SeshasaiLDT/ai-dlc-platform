"""Deterministic in-memory membership adapter for tests and local use."""

from ai_dlc.domain.identity import (
    InitiativeMembership,
    MembershipDisabledError,
    MembershipNotFoundError,
)


class InMemoryMembershipRepository:
    def __init__(self, memberships: tuple[InitiativeMembership, ...] = ()) -> None:
        records: dict[tuple[str, str], InitiativeMembership] = {}
        for membership in memberships:
            key = (membership.principal_id, membership.initiative_id)
            if key in records:
                raise ValueError("duplicate principal/initiative membership")
            records[key] = membership
        self._records = records

    def get_membership(self, principal_id: str, initiative_id: str) -> InitiativeMembership:
        membership = self._records.get((principal_id, initiative_id))
        if membership is None:
            raise MembershipNotFoundError(principal_id, initiative_id)
        if not membership.enabled:
            raise MembershipDisabledError(principal_id, initiative_id)
        return membership

    def list_memberships(self, principal_id: str) -> tuple[InitiativeMembership, ...]:
        return tuple(
            sorted(
                (item for item in self._records.values() if item.principal_id == principal_id),
                key=lambda item: item.initiative_id,
            )
        )
