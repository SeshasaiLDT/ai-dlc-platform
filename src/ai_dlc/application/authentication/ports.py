"""Provider validation and normalization boundary."""

from typing import Protocol

from .models import BearerCredential, ValidatedIdentityClaims


class AuthenticationProvider(Protocol):
    def validate(self, credential: BearerCredential) -> ValidatedIdentityClaims:
        """Verify credential and normalize identity, or raise AuthenticationError.

        A production adapter must verify signature, trusted issuer, audience,
        expiry, not-before, and required subject before returning claims.
        Provider-specific claims and raw credentials stay inside the adapter.
        """
