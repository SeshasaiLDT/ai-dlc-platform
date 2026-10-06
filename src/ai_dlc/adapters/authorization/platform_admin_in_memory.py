"""In-memory global platform-admin assignments for tests and local use."""


class InMemoryPlatformAdminRepository:
    def __init__(self, principal_ids: frozenset[str] = frozenset()) -> None:
        ids = frozenset(principal_ids)
        if any(not isinstance(value, str) or not value.strip() for value in ids):
            raise ValueError("platform admin principal IDs must be nonblank")
        self._principal_ids = ids

    def is_platform_admin(self, principal_id: str) -> bool:
        return principal_id in self._principal_ids
