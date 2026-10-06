"""Minimal, immutable values crossing the authentication boundary."""

from dataclasses import dataclass, field

from ai_dlc.domain.identity import Principal

from .errors import MissingCredentialsError


@dataclass(frozen=True, slots=True)
class BearerCredential:
    """Transient credential input; its representation never contains the secret."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise MissingCredentialsError


@dataclass(frozen=True, slots=True)
class ValidatedIdentityClaims:
    """Only normalized fields returned by a trusted provider adapter."""

    subject_id: str | None
    provider: str
    display_name: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedRequestContext:
    """Backend propagation value; no credentials or authorization grants."""

    principal: Principal

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be a Principal")
