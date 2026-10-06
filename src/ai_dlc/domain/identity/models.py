"""Immutable enterprise principal and initiative membership contracts."""

from dataclasses import dataclass

from .enums import Role
from .errors import InvalidRoleError


def _nonblank(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")


@dataclass(frozen=True, slots=True)
class Principal:
    subject_id: str
    provider: str
    display_name: str | None = None
    email: str | None = None

    def __post_init__(self) -> None:
        _nonblank(self.subject_id, "subject_id")
        _nonblank(self.provider, "provider")


@dataclass(frozen=True, slots=True)
class InitiativeMembership:
    principal_id: str
    initiative_id: str
    roles: tuple[Role, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        _nonblank(self.principal_id, "principal_id")
        _nonblank(self.initiative_id, "initiative_id")
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a bool")
        try:
            roles = tuple(Role(role) for role in self.roles)
        except (ValueError, TypeError) as exc:
            raise InvalidRoleError(self.roles) from exc
        object.__setattr__(self, "roles", tuple(dict.fromkeys(roles)))
