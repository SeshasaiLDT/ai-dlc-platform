"""Public authentication failures without credential-bearing diagnostics."""


class AuthenticationError(Exception):
    """Expected failure to authenticate a request."""


class AuthenticationProviderError(Exception):
    """Unexpected provider failure; distinct from unauthenticated requests."""

    def __init__(self) -> None:
        super().__init__("authentication provider failed")


class MissingCredentialsError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("authentication credentials are missing")


class InvalidCredentialsError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("authentication credentials are invalid")


class ExpiredCredentialsError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("authentication credentials have expired")


class InvalidIssuerError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("credential issuer is invalid")


class InvalidAudienceError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("credential audience is invalid")


class MissingSubjectError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("validated identity has no subject")


class InvalidClaimsError(AuthenticationError):
    def __init__(self) -> None:
        super().__init__("validated identity claims are invalid")
