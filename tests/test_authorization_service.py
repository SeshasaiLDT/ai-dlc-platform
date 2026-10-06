"""Security behavior of the centralized application authorization boundary."""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dlc.adapters.authentication import DeterministicTestProvider
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.application.authentication import AuthenticationService, BearerCredential
from ai_dlc.application.authorization import (
    AdminAction,
    AuthorizationAuditError,
    AuthorizationDeniedError,
    AuthorizationReason,
    AuthorizationRequest,
    AuthorizationService,
    CapabilityAction,
    JiraProjectTarget,
    KnowledgeSourceTarget,
    RepositoryTarget,
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
    ToolAction,
)
from ai_dlc.domain.identity import (
    AdminPermission,
    Capability,
    InitiativeMembership,
    Principal,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import load_initiative_profile

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


@pytest.fixture
def profile():
    return load_initiative_profile(EXAMPLES / "travel-platform.yaml")


@pytest.fixture
def principal() -> Principal:
    return Principal("stable-123", provider="enterprise", email="private@example.test")


@pytest.fixture
def policy() -> RolePolicy:
    return RolePolicy(
        (
            RoleGrant(
                Role.ANALYST,
                capabilities=frozenset({Capability.INVESTIGATION}),
                tool_permissions=frozenset(
                    {
                        ToolPermission.JIRA_READ,
                        ToolPermission.GIT_READ,
                        ToolPermission.KNOWLEDGE_READ,
                        ToolPermission.ARTIFACT_READ,
                    }
                ),
            ),
            RoleGrant(
                Role.INITIATIVE_ADMIN,
                admin_permissions=frozenset({AdminPermission.INITIATIVE_MEMBERSHIP_MANAGE}),
            ),
            RoleGrant(
                Role.PLATFORM_ADMIN,
                admin_permissions=frozenset({AdminPermission.PLATFORM_MANAGE}),
            ),
        )
    )


def setup_service(
    principal: Principal,
    policy: RolePolicy,
    roles: tuple[Role, ...] = (Role.ANALYST,),
    *,
    enabled: bool = True,
) -> tuple[AuthorizationService, InMemoryAuthorizationAuditSink]:
    memberships = InMemoryMembershipRepository(
        (InitiativeMembership(principal.subject_id, "travel-platform", roles, enabled),)
    )
    sink = InMemoryAuthorizationAuditSink()
    service = AuthorizationService(
        memberships,
        policy,
        sink,
        clock=lambda: NOW,
        decision_id_factory=lambda: "decision-001",
    )
    return service, sink


def request(
    principal: Principal,
    action: CapabilityAction | ToolAction | AdminAction,
    target: JiraProjectTarget | RepositoryTarget | KnowledgeSourceTarget | None = None,
    initiative_id: str = "travel-platform",
) -> AuthorizationRequest:
    return AuthorizationRequest(principal, initiative_id, action, target)


def test_capability_grant_allows_and_missing_grant_denies(principal, policy, profile) -> None:
    service, sink = setup_service(principal, policy)
    allowed = service.evaluate(
        request(principal, CapabilityAction(Capability.INVESTIGATION)), profile=profile
    )
    denied = service.evaluate(
        request(principal, CapabilityAction(Capability.IMPLEMENTATION)), profile=profile
    )
    assert allowed.allowed and allowed.reason is AuthorizationReason.ALLOWED
    assert not denied.allowed and denied.reason is AuthorizationReason.CAPABILITY_NOT_GRANTED
    assert [event.reason for event in sink.events] == [allowed.reason, denied.reason]


@pytest.mark.parametrize(
    ("read", "write", "target"),
    [
        (ToolPermission.JIRA_READ, ToolPermission.JIRA_WRITE, JiraProjectTarget("TRAVEL")),
        (ToolPermission.GIT_READ, ToolPermission.GIT_WRITE, RepositoryTarget("travel-api")),
        (ToolPermission.ARTIFACT_READ, ToolPermission.ARTIFACT_WRITE, None),
    ],
)
def test_tool_read_never_grants_write(principal, policy, profile, read, write, target) -> None:
    service, _ = setup_service(principal, policy)
    assert service.evaluate(request(principal, ToolAction(read), target), profile=profile).allowed
    denied = service.evaluate(request(principal, ToolAction(write), target), profile=profile)
    assert denied.reason is AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED


def test_admin_permissions_remain_separate(principal, policy, profile) -> None:
    initiative_service, _ = setup_service(principal, policy, (Role.INITIATIVE_ADMIN,))
    assert initiative_service.evaluate(
        request(principal, AdminAction(AdminPermission.INITIATIVE_MEMBERSHIP_MANAGE)),
        profile=profile,
    ).allowed
    assert (
        initiative_service.evaluate(
            request(principal, AdminAction(AdminPermission.PLATFORM_MANAGE)), profile=profile
        ).reason
        is AuthorizationReason.ADMIN_PERMISSION_NOT_GRANTED
    )
    platform_service, _ = setup_service(principal, policy, (Role.PLATFORM_ADMIN,))
    assert platform_service.evaluate(
        request(principal, AdminAction(AdminPermission.PLATFORM_MANAGE)), profile=profile
    ).allowed
    assert (
        platform_service.evaluate(
            request(principal, CapabilityAction(Capability.INVESTIGATION)), profile=profile
        ).reason
        is AuthorizationReason.CAPABILITY_NOT_GRANTED
    )


def test_missing_disabled_and_unconfigured_membership_deny(principal, policy, profile) -> None:
    sink = InMemoryAuthorizationAuditSink()
    missing = AuthorizationService(InMemoryMembershipRepository(), policy, sink)
    action = request(principal, CapabilityAction(Capability.INVESTIGATION))
    assert (
        missing.evaluate(action, profile=profile).reason is AuthorizationReason.MEMBERSHIP_NOT_FOUND
    )
    disabled, _ = setup_service(principal, policy, enabled=False)
    assert (
        disabled.evaluate(action, profile=profile).reason is AuthorizationReason.MEMBERSHIP_DISABLED
    )
    unconfigured, _ = setup_service(principal, policy, (Role.DEVELOPER,))
    assert (
        unconfigured.evaluate(action, profile=profile).reason
        is AuthorizationReason.CAPABILITY_NOT_GRANTED
    )
    assert sink.events[0].allowed is False


def test_profile_mismatch_denies_before_membership_lookup(principal, policy, profile) -> None:
    class NoLookup:
        def get_membership(self, principal_id, initiative_id):
            raise AssertionError("membership should not be queried")

    sink = InMemoryAuthorizationAuditSink()
    service = AuthorizationService(NoLookup(), policy, sink)
    decision = service.evaluate(
        request(principal, CapabilityAction(Capability.INVESTIGATION), initiative_id="other"),
        profile=profile,
    )
    assert decision.reason is AuthorizationReason.INITIATIVE_MISMATCH
    assert sink.events[0].reason is AuthorizationReason.INITIATIVE_MISMATCH


@pytest.mark.parametrize(
    ("permission", "inside", "outside"),
    [
        (ToolPermission.JIRA_READ, JiraProjectTarget("TRAVEL"), JiraProjectTarget("OTHER")),
        (ToolPermission.GIT_READ, RepositoryTarget("travel-api"), RepositoryTarget("other")),
        (
            ToolPermission.KNOWLEDGE_READ,
            KnowledgeSourceTarget("architecture"),
            KnowledgeSourceTarget("other"),
        ),
    ],
)
def test_logical_target_must_be_in_configured_scope(
    principal, policy, profile, permission, inside, outside
) -> None:
    service, _ = setup_service(principal, policy)
    assert service.evaluate(
        request(principal, ToolAction(permission), inside), profile=profile
    ).allowed
    denied = service.evaluate(request(principal, ToolAction(permission), outside), profile=profile)
    assert denied.reason is AuthorizationReason.TARGET_OUT_OF_SCOPE


def test_restriction_intersection_cannot_expand_profile(principal, policy, profile) -> None:
    service, _ = setup_service(principal, policy)
    restriction = ScopeRestriction(jira_projects=frozenset({"TRAVEL", "OUTSIDE"}))
    allowed = service.evaluate(
        request(principal, ToolAction(ToolPermission.JIRA_READ), JiraProjectTarget("TRAVEL")),
        profile=profile,
        scope_restriction=restriction,
    )
    narrowed = service.evaluate(
        request(principal, ToolAction(ToolPermission.JIRA_READ), JiraProjectTarget("JOURNEY")),
        profile=profile,
        scope_restriction=restriction,
    )
    outside = service.evaluate(
        request(principal, ToolAction(ToolPermission.JIRA_READ), JiraProjectTarget("OUTSIDE")),
        profile=profile,
        scope_restriction=restriction,
    )
    assert allowed.allowed
    assert narrowed.reason is outside.reason is AuthorizationReason.TARGET_OUT_OF_SCOPE


def test_target_type_mismatch_and_missing_scoped_target_fail(principal, policy, profile) -> None:
    service, sink = setup_service(principal, policy)
    mismatch = service.evaluate(
        request(principal, ToolAction(ToolPermission.JIRA_READ), RepositoryTarget("travel-api")),
        profile=profile,
    )
    missing = service.evaluate(
        request(principal, ToolAction(ToolPermission.JIRA_READ)), profile=profile
    )
    capability_target = service.evaluate(
        request(principal, CapabilityAction(Capability.INVESTIGATION), JiraProjectTarget("TRAVEL")),
        profile=profile,
    )
    assert mismatch.reason is capability_target.reason is AuthorizationReason.INVALID_REQUEST
    assert missing.reason is AuthorizationReason.TARGET_REQUIRED
    assert len(sink.events) == 3


def test_request_cannot_carry_grants_or_unknown_actions(principal) -> None:
    with pytest.raises(TypeError):
        AuthorizationRequest(
            principal,
            "travel-platform",
            CapabilityAction(Capability.INVESTIGATION),
            tool_permissions={ToolPermission.JIRA_WRITE},
        )
    with pytest.raises(ValueError, match="unknown authorization action"):
        AuthorizationRequest(principal, "travel-platform", "jira.write")
    with pytest.raises(ValueError, match="unknown tool permission"):
        ToolAction("jira.write")
    with pytest.raises(ValueError, match="unknown authorization target"):
        AuthorizationRequest(
            principal, "travel-platform", ToolAction(ToolPermission.JIRA_READ), "TRAVEL"
        )
    with pytest.raises(ValueError, match="invalid format"):
        JiraProjectTarget("Bearer secret.payload.signature")
    with pytest.raises(ValueError, match="invalid format"):
        RepositoryTarget("https://example.test/token")


def test_audit_event_is_immutable_safe_and_revision_attributed(principal, policy, profile) -> None:
    service, sink = setup_service(principal, policy)
    decision = service.evaluate(
        request(principal, ToolAction(ToolPermission.JIRA_READ), JiraProjectTarget("TRAVEL")),
        profile=profile,
        initiative_revision=7,
    )
    event = sink.events[0]
    assert event.decision_id == decision.decision_id == "decision-001"
    assert event.occurred_at == decision.occurred_at == NOW
    assert event.principal_id == "stable-123"
    assert event.initiative_id == "travel-platform"
    assert event.action == ToolAction(ToolPermission.JIRA_READ)
    assert event.target == JiraProjectTarget("TRAVEL")
    assert event.reason is AuthorizationReason.ALLOWED
    assert event.initiative_revision == 7
    assert "private@example.test" not in repr(event)
    assert {item.name for item in fields(event)} == {item.name for item in fields(decision)}
    with pytest.raises(FrozenInstanceError):
        event.allowed = False
    with pytest.raises(FrozenInstanceError):
        decision.reason = AuthorizationReason.INVALID_REQUEST
    with pytest.raises(FrozenInstanceError):
        request(principal, CapabilityAction(Capability.INVESTIGATION)).initiative_id = "other"


def test_require_stops_protected_operation_and_raises_safe_denial(
    principal, policy, profile
) -> None:
    service, sink = setup_service(principal, policy)
    ran = False

    def protected_operation():
        nonlocal ran
        service.require(
            request(principal, ToolAction(ToolPermission.JIRA_WRITE), JiraProjectTarget("TRAVEL")),
            profile=profile,
        )
        ran = True

    with pytest.raises(AuthorizationDeniedError) as caught:
        protected_operation()
    assert not ran
    assert caught.value.decision.reason is AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED
    assert "private@example.test" not in str(caught.value)
    assert len(sink.events) == 1
    allowed = service.require(
        request(principal, CapabilityAction(Capability.INVESTIGATION)), profile=profile
    )
    assert allowed.allowed


@pytest.mark.parametrize("capability", [Capability.INVESTIGATION, Capability.IMPLEMENTATION])
def test_audit_failure_blocks_result_without_masquerading_as_denial(
    principal, policy, profile, capability
) -> None:
    class BrokenSink:
        def record(self, event):
            raise OSError("backend unavailable")

    membership = InitiativeMembership(principal.subject_id, "travel-platform", (Role.ANALYST,))
    service = AuthorizationService(
        InMemoryMembershipRepository((membership,)), policy, BrokenSink()
    )
    with pytest.raises(AuthorizationAuditError, match="audit recording failed") as caught:
        service.require(request(principal, CapabilityAction(capability)), profile=profile)
    assert not isinstance(caught.value, AuthorizationDeniedError)


def test_one_principal_has_independent_initiative_decisions(principal, policy, profile) -> None:
    other = load_initiative_profile(EXAMPLES / "field-operations.yaml")
    memberships = InMemoryMembershipRepository(
        (
            InitiativeMembership(principal.subject_id, profile.initiative.id, (Role.ANALYST,)),
            InitiativeMembership(
                principal.subject_id, other.initiative.id, (Role.INITIATIVE_ADMIN,)
            ),
        )
    )
    sink = InMemoryAuthorizationAuditSink()
    service = AuthorizationService(memberships, policy, sink)
    assert service.evaluate(
        request(principal, CapabilityAction(Capability.INVESTIGATION)), profile=profile
    ).allowed
    denied = service.evaluate(
        request(
            principal, CapabilityAction(Capability.INVESTIGATION), initiative_id=other.initiative.id
        ),
        profile=other,
    )
    assert denied.reason is AuthorizationReason.CAPABILITY_NOT_GRANTED
    assert [event.initiative_id for event in sink.events] == [
        profile.initiative.id,
        other.initiative.id,
    ]


def test_authenticated_principal_crosses_boundary_without_credential_or_claims(
    policy, profile
) -> None:
    secret = "opaque-test-credential"
    provider = DeterministicTestProvider(
        provider="enterprise",
        validated_claims={"sub": "stable-123", "groups": ["admin"], "token": secret},
    )
    principal = AuthenticationService(provider).authenticate(BearerCredential(secret))
    service, sink = setup_service(principal, policy)
    decision = service.require(
        request(principal, CapabilityAction(Capability.INVESTIGATION)), profile=profile
    )
    assert decision.allowed
    assert secret not in repr(decision)
    assert secret not in repr(sink.events)
    assert "groups" not in repr(sink.events)
