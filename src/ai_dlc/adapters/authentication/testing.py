"""Test-only deterministic provider; never validates a real token."""

from collections.abc import Mapping

from ai_dlc.application.authentication import (
    AuthenticationError,
    BearerCredential,
    ValidatedIdentityClaims,
)


class DeterministicTestProvider:
    """Model a completed provider validation, with optional typed failure.

    The caller supplies already-trusted test claims. No credential value is
    compared, retained, parsed, logged, or returned. Never use for live traffic.
    """

    def __init__(
        self,
        *,
        provider: str,
        validated_claims: Mapping[str, object],
        failure: AuthenticationError | None = None,
    ) -> None:
        self._provider = provider
        self._subject = validated_claims.get("sub")
        self._display_name = validated_claims.get("name")
        self._email = validated_claims.get("email")
        self._failure = failure
        self.calls = 0

    def validate(self, credential: BearerCredential) -> ValidatedIdentityClaims:
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return ValidatedIdentityClaims(
            subject_id=self._subject,
            provider=self._provider,
            display_name=self._display_name,
            email=self._email,
        )
