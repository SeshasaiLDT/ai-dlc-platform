"""Authentication ends when the existing Principal has been established."""

from ai_dlc.domain.identity import Principal

from .errors import (
    AuthenticationError,
    AuthenticationProviderError,
    ExpiredCredentialsError,
    InvalidAudienceError,
    InvalidClaimsError,
    InvalidCredentialsError,
    InvalidIssuerError,
    MissingCredentialsError,
    MissingSubjectError,
)
from .models import AuthenticatedRequestContext, BearerCredential, ValidatedIdentityClaims
from .ports import AuthenticationProvider


class AuthenticationService:
    def __init__(self, provider: AuthenticationProvider) -> None:
        self._provider = provider

    def authenticate(self, credential: BearerCredential | None) -> Principal:
        if credential is None:
            raise MissingCredentialsError
        if not isinstance(credential, BearerCredential):
            raise TypeError("credential must be a BearerCredential")
        try:
            claims = self._provider.validate(credential)
        except (
            InvalidCredentialsError,
            ExpiredCredentialsError,
            InvalidIssuerError,
            InvalidAudienceError,
            MissingSubjectError,
            InvalidClaimsError,
        ) as exc:
            # Construct a fresh error so adapter diagnostics cannot reach callers.
            safe_type = next(
                kind
                for kind in (
                    InvalidCredentialsError,
                    ExpiredCredentialsError,
                    InvalidIssuerError,
                    InvalidAudienceError,
                    MissingSubjectError,
                    InvalidClaimsError,
                )
                if isinstance(exc, kind)
            )
            raise safe_type() from None
        except AuthenticationError:
            raise InvalidCredentialsError from None
        except Exception:
            raise AuthenticationProviderError from None
        if not isinstance(claims, ValidatedIdentityClaims):
            raise AuthenticationProviderError
        if not isinstance(claims.subject_id, str) or not claims.subject_id.strip():
            raise MissingSubjectError
        if not isinstance(claims.provider, str) or not claims.provider.strip():
            raise InvalidClaimsError
        if claims.display_name is not None and not isinstance(claims.display_name, str):
            raise InvalidClaimsError
        if claims.email is not None and not isinstance(claims.email, str):
            raise InvalidClaimsError
        return Principal(
            subject_id=claims.subject_id,
            provider=claims.provider.strip().lower(),
            display_name=claims.display_name,
            email=claims.email,
        )

    def authenticate_request(
        self, credential: BearerCredential | None
    ) -> AuthenticatedRequestContext:
        return AuthenticatedRequestContext(self.authenticate(credential))
