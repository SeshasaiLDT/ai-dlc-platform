"""Expected identity and membership failures."""


class IdentityError(Exception):
    """Base class for identity contract errors."""


class InvalidRoleError(IdentityError, ValueError):
    def __init__(self, value: object) -> None:
        super().__init__(f"unknown role: {value!r}")


class MembershipNotFoundError(IdentityError):
    def __init__(self, principal_id: str, initiative_id: str) -> None:
        super().__init__(
            f"membership for principal '{principal_id}' in '{initiative_id}' was not found"
        )


class MembershipDisabledError(IdentityError):
    def __init__(self, principal_id: str, initiative_id: str) -> None:
        super().__init__(
            f"membership for principal '{principal_id}' in '{initiative_id}' is disabled"
        )
