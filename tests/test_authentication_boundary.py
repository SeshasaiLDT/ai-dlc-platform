"""Authentication establishes identity without resolving authorization."""

from dataclasses import FrozenInstanceError, fields

import pytest

from ai_dlc.adapters.authentication import DeterministicTestProvider
from ai_dlc.application.authentication import (
    AuthenticatedRequestContext,
    AuthenticationError,
    AuthenticationProviderError,
    AuthenticationService,
    BearerCredential,
    ExpiredCredentialsError,
    InvalidAudienceError,
    InvalidClaimsError,
    InvalidCredentialsError,
    InvalidIssuerError,
    MissingCredentialsError,
    MissingSubjectError,
    ValidatedIdentityClaims,
)
from ai_dlc.domain.identity import Principal

SECRET = "opaque-sensitive-credential-987"


def provider(
    name: str = "enterprise",
    claims: dict[str, object] | None = None,
    failure: AuthenticationError | None = None,
) -> DeterministicTestProvider:
    return DeterministicTestProvider(
        provider=name,
        validated_claims=claims
        if claims is not None
        else {"sub": "stable-123", "name": "Old Name", "email": "old@example.test"},
        failure=failure,
    )


def test_valid_claims_map_to_existing_principal_without_provider_extras() -> None:
    adapter = provider(
        " Provider-One ",
        {
            "sub": "stable-123",
            "name": "Person",
            "email": "person@example.test",
            "groups": ["untrusted-group"],
            "department": "engineering",
            "access_token": SECRET,
        },
    )
    principal = AuthenticationService(adapter).authenticate(BearerCredential(SECRET))
    assert principal == Principal(
        subject_id="stable-123",
        provider="provider-one",
        display_name="Person",
        email="person@example.test",
    )
    assert {item.name for item in fields(principal)} == {
        "subject_id",
        "provider",
        "display_name",
        "email",
    }
    assert SECRET not in repr(principal)
    assert SECRET not in repr(adapter.__dict__)
    assert adapter.calls == 1


def test_subject_is_stable_when_descriptive_claims_change() -> None:
    first = AuthenticationService(provider()).authenticate(BearerCredential("first"))
    second = AuthenticationService(
        provider(claims={"sub": "stable-123", "name": "New Name", "email": "new@test"})
    ).authenticate(BearerCredential("second"))
    assert first.subject_id == second.subject_id
    assert first.display_name != second.display_name
    assert first.email != second.email


def test_multiple_provider_adapters_produce_same_principal_contract() -> None:
    one = AuthenticationService(provider("provider-one")).authenticate(BearerCredential("one"))
    two = AuthenticationService(provider("provider-two")).authenticate(BearerCredential("two"))
    assert isinstance(one, Principal) and isinstance(two, Principal)
    assert one.subject_id == two.subject_id
    assert {item.name for item in fields(one)} == {item.name for item in fields(two)}
    assert one.provider == "provider-one"
    assert two.provider == "provider-two"


@pytest.mark.parametrize("subject", [None, "", "   "])
def test_missing_or_blank_subject_fails_after_validation(subject: object) -> None:
    adapter = provider(claims={"sub": subject})
    with pytest.raises(MissingSubjectError, match="subject"):
        AuthenticationService(adapter).authenticate(BearerCredential(SECRET))


def test_missing_credentials_rejected_before_provider_call() -> None:
    adapter = provider()
    service = AuthenticationService(adapter)
    with pytest.raises(MissingCredentialsError):
        service.authenticate(None)
    with pytest.raises(MissingCredentialsError):
        BearerCredential("  ")
    assert adapter.calls == 0


@pytest.mark.parametrize(
    "failure",
    [
        InvalidCredentialsError(),
        ExpiredCredentialsError(),
        InvalidIssuerError(),
        InvalidAudienceError(),
    ],
)
def test_typed_validation_failures_do_not_expose_credentials(failure: AuthenticationError) -> None:
    adapter = provider(failure=failure)
    with pytest.raises(type(failure)) as caught:
        AuthenticationService(adapter).authenticate(BearerCredential(SECRET))
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert adapter.calls == 1


def test_credential_repr_and_context_never_contain_secret() -> None:
    credential = BearerCredential(SECRET)
    assert SECRET not in repr(credential)
    assert SECRET not in str(credential)
    assert credential.value == SECRET
    context = AuthenticationService(provider()).authenticate_request(credential)
    assert context.principal.subject_id == "stable-123"
    assert SECRET not in repr(context)
    assert SECRET not in repr(AuthenticationService(provider()).__dict__)
    assert {item.name for item in fields(context)} == {"principal"}
    with pytest.raises(FrozenInstanceError):
        context.principal = Principal("other", provider="enterprise")


def test_invalid_normalized_claims_fail_cleanly() -> None:
    with pytest.raises(InvalidClaimsError):
        AuthenticationService(provider(name=" ")).authenticate(BearerCredential(SECRET))
    with pytest.raises(InvalidClaimsError):
        AuthenticationService(provider(claims={"sub": "stable", "email": 42})).authenticate(
            BearerCredential(SECRET)
        )


def test_adapter_error_diagnostics_are_not_exposed() -> None:
    adapter = provider(failure=AuthenticationError(SECRET))
    with pytest.raises(InvalidCredentialsError) as caught:
        AuthenticationService(adapter).authenticate(BearerCredential(SECRET))
    assert SECRET not in str(caught.value)


def test_service_uses_protocol_and_does_not_resolve_membership_or_resources() -> None:
    class MinimalProvider:
        def validate(self, credential: BearerCredential) -> ValidatedIdentityClaims:
            return ValidatedIdentityClaims("stable-123", "enterprise")

    principal = AuthenticationService(MinimalProvider()).authenticate(BearerCredential(SECRET))
    assert isinstance(principal, Principal)
    assert not hasattr(principal, "roles")
    assert not hasattr(principal, "initiative_id")
    assert not hasattr(principal, "capabilities")
    assert not hasattr(principal, "tool_permissions")
    assert not hasattr(principal, "jira_projects")
    assert not hasattr(principal, "repository_ids")
    assert not hasattr(principal, "knowledge_source_ids")
    assert not hasattr(principal, "resource_bindings")


def test_provider_contract_rejects_unvalidated_output() -> None:
    class BrokenProvider:
        def validate(self, credential: BearerCredential) -> dict[str, str]:
            return {"sub": "looks-valid"}

    with pytest.raises(AuthenticationProviderError, match="provider failed"):
        AuthenticationService(BrokenProvider()).authenticate(BearerCredential(SECRET))


def test_unexpected_adapter_failure_is_distinct_and_sanitized() -> None:
    class BrokenProvider:
        def validate(self, credential: BearerCredential) -> ValidatedIdentityClaims:
            raise RuntimeError(SECRET)

    with pytest.raises(AuthenticationProviderError) as caught:
        AuthenticationService(BrokenProvider()).authenticate(BearerCredential(SECRET))
    assert not isinstance(caught.value, AuthenticationError)
    assert SECRET not in str(caught.value)


def test_validated_claims_and_context_are_immutable() -> None:
    claims = ValidatedIdentityClaims("stable", "enterprise")
    with pytest.raises(FrozenInstanceError):
        claims.subject_id = "other"
    with pytest.raises(TypeError, match="Principal"):
        AuthenticatedRequestContext("raw-claim")
