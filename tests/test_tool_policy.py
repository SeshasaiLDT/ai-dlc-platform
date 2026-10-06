"""Behavioral and security tests for operation-level tool policy."""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink, InMemoryToolPolicyRepository
from ai_dlc.application.authorization import (
    AdminAction,
    AuthorizationDecision,
    AuthorizationReason,
    AuthorizationService,
    CapabilityAction,
    JiraProjectTarget,
    PlatformAuthorizationRequest,
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
    ToolAction,
)
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    JiraOperation,
    ServiceNowOperation,
    ServiceNowTarget,
    ToolApprovalRequiredError,
    ToolKind,
    ToolOperationRisk,
    ToolPolicy,
    ToolPolicyAuditError,
    ToolPolicyConfigurationError,
    ToolPolicyDeniedError,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
    ToolPolicyService,
    operation_risk,
    required_permission,
    tool_kind,
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
NOW = datetime(2026, 10, 6, 18, tzinfo=UTC)
PRINCIPAL = Principal("subject-123", provider="enterprise", email="private@example.test")
TRAVEL = load_initiative_profile(EXAMPLES / "travel-platform.yaml")
OPERATIONS = load_initiative_profile(EXAMPLES / "field-operations.yaml")


def with_write_profile(profile, *, jira=False, git=False, servicenow=False):
    """A validated synthetic profile with selected coarse writes enabled."""
    document = profile.model_dump(mode="json")
    for enabled, name in (
        (jira, "jira_write"),
        (git, "git_write"),
        (servicenow, "servicenow_write"),
    ):
        if enabled:
            document["policies"][name] = {"enabled": True, "human_approval_required": False}
    if git:
        for repository in document["integrations"]["git"]["repositories"]:
            repository["access"] = "read_write"
    return type(profile).model_validate(document)


TRAVEL_WRITES = with_write_profile(TRAVEL, jira=True, git=True)
OPERATIONS_WRITES = with_write_profile(OPERATIONS, git=True, servicenow=True)


def role_policy() -> RolePolicy:
    return RolePolicy(
        (
            RoleGrant(
                Role.VIEWER,
                tool_permissions=frozenset(
                    {
                        ToolPermission.JIRA_READ,
                        ToolPermission.GIT_READ,
                        ToolPermission.SERVICENOW_READ,
                    }
                ),
            ),
            RoleGrant(
                Role.DEVELOPER,
                tool_permissions=frozenset(
                    {
                        ToolPermission.JIRA_READ,
                        ToolPermission.JIRA_WRITE,
                        ToolPermission.GIT_READ,
                        ToolPermission.GIT_WRITE,
                        ToolPermission.SERVICENOW_READ,
                        ToolPermission.SERVICENOW_WRITE,
                    }
                ),
            ),
        )
    )


def layers(
    policies: tuple[ToolPolicy, ...] = (),
    *,
    roles: tuple[Role, ...] = (Role.DEVELOPER,),
    both_initiatives: bool = False,
):
    memberships = [InitiativeMembership(PRINCIPAL.subject_id, TRAVEL.initiative.id, roles)]
    if both_initiatives:
        memberships.append(
            InitiativeMembership(PRINCIPAL.subject_id, OPERATIONS.initiative.id, roles)
        )
    base_audit = InMemoryAuthorizationAuditSink()
    base = AuthorizationService(
        InMemoryMembershipRepository(tuple(memberships)),
        role_policy(),
        base_audit,
        clock=lambda: NOW,
        decision_id_factory=lambda: "base-001",
    )
    tool_audit = InMemoryToolPolicyAuditSink()
    service = ToolPolicyService(
        base,
        InMemoryToolPolicyRepository(policies),
        tool_audit,
        clock=lambda: NOW,
        decision_id_factory=lambda: "policy-001",
    )
    return service, base_audit, tool_audit


def jira(operation: JiraOperation, project: str = "TRAVEL") -> ToolPolicyRequest:
    return ToolPolicyRequest(PRINCIPAL, TRAVEL.initiative.id, operation, JiraProjectTarget(project))


def git(
    operation: GitOperation,
    repository: str = "travel-api",
    branch: str | None = None,
    initiative: str = "travel-platform",
) -> ToolPolicyRequest:
    return ToolPolicyRequest(PRINCIPAL, initiative, operation, GitTarget(repository, branch))


def snow(operation: ServiceNowOperation, scope: str = "incident") -> ToolPolicyRequest:
    return ToolPolicyRequest(
        PRINCIPAL, OPERATIONS.initiative.id, operation, ServiceNowTarget(scope)
    )


def test_jira_read_allow_and_missing_policy_default_deny() -> None:
    service, base_audit, tool_audit = layers(
        (ToolPolicy("travel-platform", JiraOperation.READ_ISSUE, ToolPolicyEffect.ALLOW),)
    )
    allowed = service.evaluate(jira(JiraOperation.READ_ISSUE), profile=TRAVEL)
    missing = service.evaluate(jira(JiraOperation.SEARCH), profile=TRAVEL)
    assert allowed.effect is ToolPolicyEffect.ALLOW
    assert allowed.reason is ToolPolicyReason.ALLOWED_BY_POLICY
    assert missing.effect is ToolPolicyEffect.DENY
    assert missing.reason is ToolPolicyReason.NO_POLICY
    assert len(base_audit.events) == len(tool_audit.events) == 2


def test_narrowed_base_scope_cannot_be_expanded_by_allow_policy() -> None:
    service, base_audit, _ = layers(
        (ToolPolicy("travel-platform", JiraOperation.READ_ISSUE, ToolPolicyEffect.ALLOW),)
    )
    decision = service.evaluate(
        jira(JiraOperation.READ_ISSUE),
        profile=TRAVEL,
        scope_restriction=ScopeRestriction(jira_projects=frozenset({"JOURNEY"})),
    )
    assert decision.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    assert base_audit.events[0].reason is AuthorizationReason.TARGET_OUT_OF_SCOPE


def test_jira_write_requires_base_write_even_with_allow_policy() -> None:
    service, base_audit, _ = layers(
        (ToolPolicy("travel-platform", JiraOperation.ADD_COMMENT, ToolPolicyEffect.ALLOW),),
        roles=(Role.VIEWER,),
    )
    decision = service.evaluate(jira(JiraOperation.ADD_COMMENT), profile=TRAVEL)
    assert decision.effect is ToolPolicyEffect.DENY
    assert decision.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    assert base_audit.events[0].action == ToolAction(ToolPermission.JIRA_WRITE)
    assert not base_audit.events[0].allowed


def test_jira_delete_denied_and_comment_can_require_approval() -> None:
    service, _, _ = layers(
        (
            ToolPolicy("travel-platform", JiraOperation.DELETE_ISSUE, ToolPolicyEffect.DENY),
            ToolPolicy(
                "travel-platform", JiraOperation.ADD_COMMENT, ToolPolicyEffect.REQUIRE_APPROVAL
            ),
        )
    )
    deleted = service.evaluate(jira(JiraOperation.DELETE_ISSUE), profile=TRAVEL_WRITES)
    comment = service.evaluate(jira(JiraOperation.ADD_COMMENT), profile=TRAVEL_WRITES)
    assert deleted.reason is ToolPolicyReason.DESTRUCTIVE_OPERATION_DENIED
    assert comment.effect is ToolPolicyEffect.REQUIRE_APPROVAL
    assert comment.reason is ToolPolicyReason.APPROVAL_REQUIRED


def test_git_read_allow_push_requires_write_and_delete_has_no_inherited_policy() -> None:
    policies = (
        ToolPolicy("travel-platform", GitOperation.READ_REPOSITORY, ToolPolicyEffect.ALLOW),
        ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW),
    )
    viewer, _, _ = layers(policies, roles=(Role.VIEWER,))
    assert (
        viewer.evaluate(git(GitOperation.READ_REPOSITORY), profile=TRAVEL).effect
        is ToolPolicyEffect.ALLOW
    )
    denied = viewer.evaluate(git(GitOperation.PUSH, branch="feature/change"), profile=TRAVEL)
    assert denied.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    developer, _, _ = layers(policies)
    missing = developer.evaluate(
        git(GitOperation.DELETE_BRANCH, branch="feature/change"), profile=TRAVEL_WRITES
    )
    assert missing.risk is ToolOperationRisk.DESTRUCTIVE
    assert missing.reason is ToolPolicyReason.NO_POLICY


def test_git_branch_rules_allow_deny_and_require_approval() -> None:
    service, _, _ = layers(
        (
            ToolPolicy(
                "travel-platform",
                GitOperation.PUSH,
                ToolPolicyEffect.ALLOW,
                branch_pattern="feature/*",
            ),
            ToolPolicy(
                "travel-platform", GitOperation.PUSH, ToolPolicyEffect.DENY, branch_pattern="main"
            ),
            ToolPolicy(
                "travel-platform",
                GitOperation.PUSH,
                ToolPolicyEffect.REQUIRE_APPROVAL,
                branch_pattern="release/*",
            ),
        )
    )
    assert (
        service.evaluate(
            git(GitOperation.PUSH, branch="feature/change"), profile=TRAVEL_WRITES
        ).effect
        is ToolPolicyEffect.ALLOW
    )
    assert (
        service.evaluate(git(GitOperation.PUSH, branch="main"), profile=TRAVEL_WRITES).effect
        is ToolPolicyEffect.DENY
    )
    assert (
        service.evaluate(git(GitOperation.PUSH, branch="release/v1"), profile=TRAVEL_WRITES).effect
        is ToolPolicyEffect.REQUIRE_APPROVAL
    )
    assert (
        service.evaluate(
            git(GitOperation.PUSH, branch="bugfix/change"), profile=TRAVEL_WRITES
        ).reason
        is ToolPolicyReason.TARGET_NOT_ALLOWED
    )


def test_exact_repository_policy_restricts_git_operation() -> None:
    service, _, _ = layers(
        (
            ToolPolicy(
                "travel-platform",
                GitOperation.READ_REPOSITORY,
                ToolPolicyEffect.ALLOW,
                target_id="travel-api",
            ),
        )
    )
    assert (
        service.require_allowed(git(GitOperation.READ_REPOSITORY), profile=TRAVEL).effect
        is ToolPolicyEffect.ALLOW
    )
    denied = service.evaluate(
        git(GitOperation.READ_REPOSITORY, repository="travel-web"), profile=TRAVEL
    )
    assert denied.reason is ToolPolicyReason.TARGET_NOT_ALLOWED


def test_servicenow_read_write_are_distinct_and_scope_is_logical() -> None:
    policies = (
        ToolPolicy("field-operations", ServiceNowOperation.READ_RECORD, ToolPolicyEffect.ALLOW),
        ToolPolicy("field-operations", ServiceNowOperation.UPDATE_RECORD, ToolPolicyEffect.ALLOW),
    )
    viewer, _, _ = layers(policies, roles=(Role.VIEWER,), both_initiatives=True)
    assert (
        viewer.evaluate(snow(ServiceNowOperation.READ_RECORD), profile=OPERATIONS).effect
        is ToolPolicyEffect.ALLOW
    )
    assert (
        viewer.evaluate(snow(ServiceNowOperation.UPDATE_RECORD), profile=OPERATIONS).reason
        is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    )
    developer, _, _ = layers(policies, both_initiatives=True)
    assert (
        developer.evaluate(
            snow(ServiceNowOperation.READ_RECORD, "unknown"), profile=OPERATIONS
        ).reason
        is ToolPolicyReason.TARGET_NOT_ALLOWED
    )
    assert (
        developer.evaluate(snow(ServiceNowOperation.SEARCH), profile=OPERATIONS).reason
        is ToolPolicyReason.NO_POLICY
    )


def test_existing_initiative_write_switches_and_read_only_repositories_are_upper_bounds() -> None:
    service, _, _ = layers(
        (
            ToolPolicy("travel-platform", JiraOperation.ADD_COMMENT, ToolPolicyEffect.ALLOW),
            ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW),
        )
    )
    jira_denied = service.evaluate(jira(JiraOperation.ADD_COMMENT), profile=TRAVEL)
    readonly_denied = service.evaluate(
        git(GitOperation.PUSH, repository="travel-web", branch="feature/change"),
        profile=TRAVEL,
    )
    assert jira_denied.reason is ToolPolicyReason.INITIATIVE_POLICY_DISABLED
    assert readonly_denied.reason is ToolPolicyReason.TARGET_NOT_ALLOWED


def test_existing_profile_approval_flag_can_only_restrict_explicit_allow() -> None:
    git_service, _, _ = layers(
        (ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW),)
    )
    git_decision = git_service.evaluate(
        git(GitOperation.PUSH, branch="feature/change"), profile=TRAVEL
    )
    assert git_decision.effect is ToolPolicyEffect.REQUIRE_APPROVAL
    assert git_decision.reason is ToolPolicyReason.APPROVAL_REQUIRED

    snow_service, _, _ = layers(
        (
            ToolPolicy(
                "field-operations", ServiceNowOperation.UPDATE_RECORD, ToolPolicyEffect.ALLOW
            ),
        ),
        both_initiatives=True,
    )
    snow_decision = snow_service.evaluate(
        snow(ServiceNowOperation.UPDATE_RECORD), profile=OPERATIONS
    )
    assert snow_decision.effect is ToolPolicyEffect.REQUIRE_APPROVAL


def test_same_role_and_base_permission_have_different_initiative_policy() -> None:
    policies = (
        ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW),
        ToolPolicy("field-operations", GitOperation.PUSH, ToolPolicyEffect.REQUIRE_APPROVAL),
    )
    service, base_audit, _ = layers(policies, both_initiatives=True)
    travel = service.evaluate(
        git(GitOperation.PUSH, branch="feature/change"), profile=TRAVEL_WRITES
    )
    operations = service.evaluate(
        git(GitOperation.PUSH, "operations-runbooks", "feature/change", "field-operations"),
        profile=OPERATIONS_WRITES,
    )
    assert travel.effect is ToolPolicyEffect.ALLOW
    assert operations.effect is ToolPolicyEffect.REQUIRE_APPROVAL
    assert all(event.allowed for event in base_audit.events)


def test_ambiguous_matching_rules_deny() -> None:
    service, _, _ = layers(
        (
            ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW),
            ToolPolicy(
                "travel-platform", GitOperation.PUSH, ToolPolicyEffect.DENY, branch_pattern="main"
            ),
        )
    )
    decision = service.evaluate(git(GitOperation.PUSH, branch="main"), profile=TRAVEL_WRITES)
    assert decision.effect is ToolPolicyEffect.DENY
    assert decision.reason is ToolPolicyReason.AMBIGUOUS_POLICY


def test_enforcement_distinguishes_deny_and_approval_without_execution() -> None:
    service, _, _ = layers(
        (
            ToolPolicy("travel-platform", JiraOperation.DELETE_ISSUE, ToolPolicyEffect.DENY),
            ToolPolicy(
                "travel-platform", JiraOperation.ADD_COMMENT, ToolPolicyEffect.REQUIRE_APPROVAL
            ),
        )
    )
    ran = False

    def protected(request: ToolPolicyRequest) -> None:
        nonlocal ran
        service.require_allowed(request, profile=TRAVEL_WRITES)
        ran = True

    with pytest.raises(ToolPolicyDeniedError) as denied:
        protected(jira(JiraOperation.DELETE_ISSUE))
    with pytest.raises(ToolApprovalRequiredError) as approval:
        protected(jira(JiraOperation.ADD_COMMENT))
    assert not ran
    assert denied.value.decision.effect is ToolPolicyEffect.DENY
    assert approval.value.decision.effect is ToolPolicyEffect.REQUIRE_APPROVAL
    assert "private@example.test" not in str(denied.value)
    assert "private@example.test" not in str(approval.value)


def test_audit_references_base_decision_and_excludes_descriptive_identity() -> None:
    service, base_audit, tool_audit = layers(
        (ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW),)
    )
    decision = service.evaluate(
        git(GitOperation.PUSH, branch="feature/change"), profile=TRAVEL_WRITES
    )
    event = tool_audit.events[0]
    assert event.base_decision_id == base_audit.events[0].decision_id == "base-001"
    assert event.decision_id == decision.decision_id == "policy-001"
    assert event.principal_id == PRINCIPAL.subject_id
    assert event.initiative_id == TRAVEL.initiative.id
    assert event.tool is ToolKind.GIT
    assert event.operation is GitOperation.PUSH
    assert event.risk is ToolOperationRisk.WRITE
    assert event.effect is ToolPolicyEffect.ALLOW
    assert event.occurred_at == NOW
    assert "private@example.test" not in repr(event)
    assert {item.name for item in fields(event)} == {item.name for item in fields(decision)}
    with pytest.raises(FrozenInstanceError):
        event.effect = ToolPolicyEffect.DENY
    with pytest.raises(FrozenInstanceError):
        decision.effect = ToolPolicyEffect.DENY


def test_request_and_rule_are_immutable_and_cannot_carry_grants() -> None:
    policy = ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW)
    req = git(GitOperation.PUSH, branch="feature/change")
    with pytest.raises(FrozenInstanceError):
        policy.effect = ToolPolicyEffect.DENY
    with pytest.raises(FrozenInstanceError):
        req.operation = GitOperation.DELETE_BRANCH
    with pytest.raises(TypeError):
        ToolPolicyRequest(
            PRINCIPAL,
            "travel-platform",
            GitOperation.PUSH,
            GitTarget("travel-api"),
            authorization_decision="ALLOW",
        )
    with pytest.raises(ValueError, match="logical target"):
        ToolPolicyRequest(
            PRINCIPAL, "travel-platform", JiraOperation.SEARCH, GitTarget("travel-api")
        )


def test_base_proof_cannot_be_substituted_for_wrong_tool_target_or_action() -> None:
    class FakeBase:
        def __init__(self, action, target, initiative="travel-platform"):
            self.action = action
            self.target = target
            self.initiative = initiative

        def evaluate(self, request, **kwargs):
            return AuthorizationDecision(
                "fake-base",
                NOW,
                PRINCIPAL.subject_id,
                self.initiative,
                self.action,
                self.target,
                True,
                AuthorizationReason.ALLOWED,
            )

    policy = InMemoryToolPolicyRepository(
        (ToolPolicy("travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW),)
    )
    for action, target, initiative in (
        (ToolAction(ToolPermission.JIRA_WRITE), None, "travel-platform"),
        (ToolAction(ToolPermission.GIT_READ), None, "travel-platform"),
        (ToolAction(ToolPermission.GIT_WRITE), None, "travel-platform"),
        (ToolAction(ToolPermission.GIT_WRITE), None, "another"),
        (CapabilityAction(Capability.IMPLEMENTATION), None, "travel-platform"),
        (AdminAction(AdminPermission.PLATFORM_MANAGE), None, "travel-platform"),
    ):
        service = ToolPolicyService(
            FakeBase(action, target, initiative), policy, InMemoryToolPolicyAuditSink()
        )
        decision = service.evaluate(
            git(GitOperation.PUSH, branch="feature/change"), profile=TRAVEL_WRITES
        )
        assert decision.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED


def test_platform_authorization_is_not_a_tool_request() -> None:
    with pytest.raises(TypeError):
        ToolPolicyRequest(
            PlatformAuthorizationRequest(PRINCIPAL),
            "travel-platform",
            GitOperation.PUSH,
            GitTarget("travel-api"),
        )


def test_policy_repository_failure_is_distinct_from_denial() -> None:
    class BrokenRepository:
        def policies_for(self, initiative_id, tool):
            raise OSError("storage unavailable")

    service, _, _ = layers()
    service._policies = BrokenRepository()
    with pytest.raises(ToolPolicyConfigurationError):
        service.evaluate(jira(JiraOperation.READ_ISSUE), profile=TRAVEL)


def test_audit_failure_blocks_execution() -> None:
    class BrokenSink:
        def record(self, event):
            raise OSError("audit unavailable")

    service, _, _ = layers(
        (ToolPolicy("travel-platform", JiraOperation.READ_ISSUE, ToolPolicyEffect.ALLOW),)
    )
    service._audit_sink = BrokenSink()
    with pytest.raises(ToolPolicyAuditError):
        service.require_allowed(jira(JiraOperation.READ_ISSUE), profile=TRAVEL)


@pytest.mark.parametrize(
    ("operation", "kind", "risk", "permission"),
    [
        (JiraOperation.READ_ISSUE, ToolKind.JIRA, ToolOperationRisk.READ, ToolPermission.JIRA_READ),
        (
            JiraOperation.ADD_COMMENT,
            ToolKind.JIRA,
            ToolOperationRisk.WRITE,
            ToolPermission.JIRA_WRITE,
        ),
        (
            JiraOperation.DELETE_ISSUE,
            ToolKind.JIRA,
            ToolOperationRisk.DESTRUCTIVE,
            ToolPermission.JIRA_WRITE,
        ),
        (GitOperation.READ_DIFF, ToolKind.GIT, ToolOperationRisk.READ, ToolPermission.GIT_READ),
        (GitOperation.PUSH, ToolKind.GIT, ToolOperationRisk.WRITE, ToolPermission.GIT_WRITE),
        (
            GitOperation.DELETE_BRANCH,
            ToolKind.GIT,
            ToolOperationRisk.DESTRUCTIVE,
            ToolPermission.GIT_WRITE,
        ),
        (
            ServiceNowOperation.READ_RECORD,
            ToolKind.SERVICENOW,
            ToolOperationRisk.READ,
            ToolPermission.SERVICENOW_READ,
        ),
        (
            ServiceNowOperation.UPDATE_RECORD,
            ToolKind.SERVICENOW,
            ToolOperationRisk.WRITE,
            ToolPermission.SERVICENOW_WRITE,
        ),
        (
            ServiceNowOperation.DELETE_RECORD,
            ToolKind.SERVICENOW,
            ToolOperationRisk.DESTRUCTIVE,
            ToolPermission.SERVICENOW_WRITE,
        ),
    ],
)
def test_operation_classification_is_static(operation, kind, risk, permission) -> None:
    assert tool_kind(operation) is kind
    assert operation_risk(operation) is risk
    assert required_permission(operation) is permission


def test_invalid_branch_and_policy_values_fail_cleanly() -> None:
    with pytest.raises(ValueError, match="branch"):
        GitTarget("travel-api", "main; skip approval")
    with pytest.raises(ValueError, match="branch pattern"):
        ToolPolicy(
            "travel-platform", GitOperation.PUSH, ToolPolicyEffect.ALLOW, branch_pattern="[a-z]+"
        )
    with pytest.raises(ValueError, match="Git operation"):
        ToolPolicy(
            "travel-platform",
            JiraOperation.SEARCH,
            ToolPolicyEffect.ALLOW,
            branch_pattern="feature/*",
        )
    with pytest.raises(ValueError, match="unknown policy effect"):
        ToolPolicy("travel-platform", GitOperation.PUSH, "allow")
    assert ServiceNowTarget("sc_request").scope_id == "sc_request"
    with pytest.raises(ValueError, match="ServiceNow scope"):
        ServiceNowTarget("https://example.test/token")
    with pytest.raises(ValueError, match="requires a branch"):
        git(GitOperation.PUSH)
    with pytest.raises(ValueError, match="requires a branch"):
        git(GitOperation.DELETE_BRANCH)


def test_cross_tool_same_named_operation_does_not_match() -> None:
    policy = ToolPolicy("field-operations", JiraOperation.SEARCH, ToolPolicyEffect.ALLOW)
    assert not policy.matches(snow(ServiceNowOperation.SEARCH))


def test_every_declared_operation_has_a_fixed_risk_and_permission() -> None:
    for enum_type in (JiraOperation, GitOperation, ServiceNowOperation):
        for operation in enum_type:
            assert isinstance(operation_risk(operation), ToolOperationRisk)
            assert isinstance(required_permission(operation), ToolPermission)
    with pytest.raises(ValueError, match="unknown tool operation"):
        operation_risk("delete_branch")


def test_policy_contracts_contain_only_logical_identifiers() -> None:
    field_names = {
        item.name
        for model in (ToolPolicy, ToolPolicyRequest, GitTarget, ServiceNowTarget)
        for item in fields(model)
    }
    assert not field_names & {
        "git_url",
        "jira_site",
        "database_endpoint",
        "s3_bucket",
        "credentials",
        "access_token",
    }
