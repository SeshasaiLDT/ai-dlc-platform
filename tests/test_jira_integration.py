"""Governed Jira/MCP boundary without live enterprise access."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dlc.adapters.approval import InMemoryApprovalRepository, InMemoryHumanIdentityVerifier
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.jira import InMemoryJiraProvider, InMemoryJiraToolAuditSink
from ai_dlc.adapters.resource_bindings import (
    InMemoryBindingEventSink,
    InMemoryResourceBindingRepository,
)
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink, InMemoryToolPolicyRepository
from ai_dlc.application.approval import ApprovalService
from ai_dlc.application.authorization import (
    AuthorizationService,
    JiraProjectTarget,
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
    resolve_authorization_context,
)
from ai_dlc.application.jira import (
    GovernedJiraService,
    JiraAuditError,
    JiraErrorCode,
    JiraIssueDetail,
    JiraMcpToolHandler,
    TrustedJiraContext,
)
from ai_dlc.application.resource_bindings import (
    JiraBinding,
    ResourceBindingKey,
    ResourceBindingRegistry,
    ResourceType,
    TrustedResolutionContext,
)
from ai_dlc.application.tool_policy import (
    JiraOperation,
    ToolPolicy,
    ToolPolicyEffect,
    ToolPolicyRequest,
    ToolPolicyService,
)
from ai_dlc.domain.identity import (
    AdminPermission,
    InitiativeMembership,
    Principal,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/initiatives/examples/travel-platform.yaml"
NOW = datetime(2026, 10, 7, tzinfo=UTC)
USER = Principal("shared-user", "test")
APPROVER = Principal("approver", "test")
READ_WRITE = frozenset({ToolPermission.JIRA_READ, ToolPermission.JIRA_WRITE})
OPERATIONS = (
    JiraOperation.READ_ISSUE,
    JiraOperation.SEARCH,
    JiraOperation.CREATE_ISSUE,
    JiraOperation.UPDATE_ISSUE,
    JiraOperation.TRANSITION_ISSUE,
    JiraOperation.ADD_COMMENT,
)


def make_profile(initiative_id: str, project: str, *, approval: bool = False) -> InitiativeProfile:
    document = load_initiative_profile(EXAMPLE).model_dump(mode="json")
    document["initiative"]["id"] = initiative_id
    document["integrations"]["jira"]["projects"] = [project]
    document["policies"]["jira_write"] = {
        "enabled": True,
        "human_approval_required": approval,
    }
    return InitiativeProfile.model_validate(document)


ALPHA = make_profile("initiative-alpha", "ALPHA")
BETA = make_profile("initiative-beta", "BETA")


def issue(key: str, summary: str = "Example") -> JiraIssueDetail:
    return JiraIssueDetail(
        issue_key=key,
        project_key=key.rsplit("-", 1)[0],
        issue_type="Task",
        summary=summary,
        status="Open",
        updated_at=NOW,
        description="Description",
    )


class World:
    def __init__(
        self,
        *,
        profiles=(ALPHA, BETA),
        permissions=READ_WRITE,
        policies=None,
        provider=None,
        approvals=False,
    ):
        self.profiles = profiles
        self.provider = provider or InMemoryJiraProvider((issue("ALPHA-1"), issue("BETA-1")))
        self.audit = InMemoryJiraToolAuditSink()
        memberships = tuple(
            InitiativeMembership(USER.subject_id, profile.initiative.id, (Role.DEVELOPER,))
            for profile in profiles
        )
        if approvals:
            memberships += (
                InitiativeMembership(
                    APPROVER.subject_id, profiles[0].initiative.id, (Role.INITIATIVE_ADMIN,)
                ),
            )
        self.memberships = InMemoryMembershipRepository(memberships)
        self.roles = RolePolicy(
            (
                RoleGrant(Role.DEVELOPER, tool_permissions=permissions),
                RoleGrant(
                    Role.INITIATIVE_ADMIN,
                    admin_permissions=frozenset({AdminPermission.INITIATIVE_APPROVAL_MANAGE}),
                ),
            )
        )
        authorization = AuthorizationService(
            self.memberships, self.roles, InMemoryAuthorizationAuditSink()
        )
        policy_rules = policies
        if policy_rules is None:
            policy_rules = tuple(
                ToolPolicy(profile.initiative.id, op, ToolPolicyEffect.ALLOW)
                for profile in profiles
                for op in OPERATIONS
            )
        self.policy = ToolPolicyService(
            authorization,
            InMemoryToolPolicyRepository(policy_rules),
            InMemoryToolPolicyAuditSink(),
        )
        self.bindings = ResourceBindingRegistry(
            InMemoryResourceBindingRepository(),
            InMemoryBindingEventSink(),
            environments=frozenset({"dev"}),
            clock=lambda: NOW,
        )
        for profile in profiles:
            self.bindings.register(
                ResourceBindingKey("dev", profile.initiative.id, ResourceType.JIRA, "jira"),
                JiraBinding(f"connection-{profile.initiative.id}", f"site-{profile.initiative.id}"),
                correlation_id="setup",
            )
        self.approvals = None
        if approvals:
            self.approvals = ApprovalService(
                self.policy,
                authorization,
                InMemoryApprovalRepository(),
                InMemoryHumanIdentityVerifier(frozenset({APPROVER.subject_id})),
                clock=lambda: NOW,
            )
        self.service = GovernedJiraService(
            self.policy, self.bindings, self.provider, self.audit, approvals=self.approvals
        )
        self.mcp = JiraMcpToolHandler(self.service)

    def context(
        self,
        profile=ALPHA,
        *,
        restriction=None,
        approval_id=None,
        cancelled=False,
    ) -> TrustedJiraContext:
        authorization = resolve_authorization_context(
            USER,
            profile.initiative.id,
            profile,
            self.memberships,
            self.roles,
            scope_restriction=restriction,
        )
        resource = TrustedResolutionContext("dev", profile, authorization, "trace-1")
        return TrustedJiraContext(
            resource,
            initiative_revision=1,
            approval_id=approval_id,
            workspace_id="workspace-1",
            task_id="task-1",
            cancelled=cancelled,
        )


def call(world: World, name: str, arguments: dict, *, context=None) -> dict:
    return world.mcp.invoke(name, arguments, context=context or world.context())


def test_get_issue_is_normalized_and_uses_trusted_binding() -> None:
    world = World()
    result = call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "ALPHA-1"})
    assert result["outcome"] == "success"
    assert result["data"]["kind"] == "issue_detail"
    assert result["data"]["issue_key"] == "ALPHA-1"
    assert result["data"]["updated_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert world.provider.calls[0].connection_alias == "connection-initiative-alpha"
    assert world.audit.events[0].project_key == "ALPHA"
    assert world.audit.events[0].principal_id == USER.subject_id
    assert "connection-initiative-alpha" not in str(result)
    assert "site-initiative-alpha" not in str(result)


def test_scope_and_issue_key_denied_before_provider_and_prompt_cannot_override() -> None:
    world = World()
    denied = call(world, "jira_get_issue", {"project_key": "BETA", "issue_key": "BETA-1"})
    mismatch = call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "BETA-1"})
    narrowed = call(
        world,
        "jira_get_issue",
        {"project_key": "ALPHA", "issue_key": "ALPHA-1"},
        context=world.context(restriction=ScopeRestriction(jira_projects=frozenset())),
    )
    assert [item["error"]["code"] for item in (denied, mismatch, narrowed)] == ["INVALID_SCOPE"] * 3
    assert world.provider.calls == []
    extra = call(world, "jira_search_issues", {"project_key": "ALPHA", "jql": "project = BETA"})
    assert extra["error"]["code"] == "INVALID_ARGUMENT"
    assert world.provider.calls == []
    literal = call(world, "jira_search_issues", {"project_key": "ALPHA", "text": "project = BETA"})
    assert literal["outcome"] == "success" and literal["data"]["issues"] == []
    assert world.provider.calls[-1].project_key == "ALPHA"


def test_search_pagination_empty_and_multi_initiative_selection() -> None:
    provider = InMemoryJiraProvider((issue("ALPHA-1"), issue("ALPHA-2"), issue("BETA-1")))
    world = World(provider=provider)
    first = call(world, "jira_search_issues", {"project_key": "ALPHA", "page_size": 1})
    second = call(
        world,
        "jira_search_issues",
        {"project_key": "ALPHA", "page_size": 1, "cursor": first["data"]["next_cursor"]},
    )
    assert [item["issue_key"] for item in first["data"]["issues"]] == ["ALPHA-1"]
    assert first["data"]["has_more"] and first["data"]["next_cursor"] == "1"
    assert [item["issue_key"] for item in second["data"]["issues"]] == ["ALPHA-2"]
    assert not second["data"]["has_more"] and "next_cursor" not in second["data"]
    empty = call(world, "jira_search_issues", {"project_key": "ALPHA", "text": "absent"})
    assert empty["data"]["issues"] == []
    beta = call(
        world,
        "jira_get_issue",
        {"project_key": "BETA", "issue_key": "BETA-1"},
        context=world.context(BETA),
    )
    assert beta["outcome"] == "success"
    wrong = call(
        world,
        "jira_get_issue",
        {"project_key": "ALPHA", "issue_key": "ALPHA-1"},
        context=world.context(BETA),
    )
    assert wrong["error"]["code"] == "INVALID_SCOPE"
    assert [item.project_key for item in provider.calls] == ["ALPHA", "ALPHA", "ALPHA", "BETA"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("jira_create_issue", {"project_key": "ALPHA", "issue_type": "Task", "summary": "New"}),
        (
            "jira_update_issue",
            {"project_key": "ALPHA", "issue_key": "ALPHA-1", "summary": "Edited"},
        ),
        (
            "jira_transition_issue",
            {"project_key": "ALPHA", "issue_key": "ALPHA-1", "transition_id": "Done"},
        ),
        ("jira_add_comment", {"project_key": "ALPHA", "issue_key": "ALPHA-1", "body": "Comment"}),
    ],
)
def test_read_permission_never_allows_write(tool, arguments) -> None:
    world = World(permissions=frozenset({ToolPermission.JIRA_READ}))
    denied = call(world, tool, arguments)
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert world.provider.calls == []


def test_write_operations_succeed_only_with_project_and_exact_policy() -> None:
    world = World()
    denied = call(
        world, "jira_create_issue", {"project_key": "BETA", "issue_type": "Task", "summary": "No"}
    )
    assert denied["error"]["code"] == "INVALID_SCOPE" and world.provider.calls == []
    created = call(
        world, "jira_create_issue", {"project_key": "ALPHA", "issue_type": "Task", "summary": "New"}
    )
    key = created["data"]["issue_key"]
    updated = call(
        world, "jira_update_issue", {"project_key": "ALPHA", "issue_key": key, "summary": "Edited"}
    )
    transitioned = call(
        world,
        "jira_transition_issue",
        {"project_key": "ALPHA", "issue_key": key, "transition_id": "Done"},
    )
    commented = call(
        world, "jira_add_comment", {"project_key": "ALPHA", "issue_key": key, "body": "Note"}
    )
    assert [item["outcome"] for item in (created, updated, transitioned, commented)] == [
        "success"
    ] * 4
    assert transitioned["data"]["status"] == "Done"
    assert commented["data"]["comment_id"].startswith("comment-")
    assert all(item.project_key == "ALPHA" for item in world.provider.calls)

    rules = (ToolPolicy(ALPHA.initiative.id, JiraOperation.READ_ISSUE, ToolPolicyEffect.ALLOW),)
    blocked = World(profiles=(ALPHA,), policies=rules)
    result = call(
        blocked,
        "jira_create_issue",
        {"project_key": "ALPHA", "issue_type": "Task", "summary": "No"},
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert blocked.provider.calls == []


def test_approval_gate_reuses_existing_service_and_rechecks_exact_request() -> None:
    profile = make_profile("initiative-alpha", "ALPHA", approval=True)
    world = World(profiles=(profile,), approvals=True)
    arguments = {"project_key": "ALPHA", "issue_key": "ALPHA-1", "body": "Approved note"}
    denied = call(world, "jira_add_comment", arguments, context=world.context(profile))
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert world.provider.calls == []
    unknown = call(
        world,
        "jira_add_comment",
        arguments,
        context=world.context(profile, approval_id="unknown-approval"),
    )
    assert unknown["error"]["code"] == "PERMISSION_DENIED"
    assert world.provider.calls == []
    policy_request = ToolPolicyRequest(
        USER, profile.initiative.id, JiraOperation.ADD_COMMENT, JiraProjectTarget("ALPHA")
    )
    record = world.approvals.request_approval(
        policy_request,
        profile=profile,
        request_key="comment-1",
        initiative_revision=1,
        scope_restriction=ScopeRestriction(jira_projects=frozenset({"ALPHA"})),
    )
    approved = world.approvals.approve(
        record.approval_id, APPROVER, profile=profile, expected_version=1
    )
    result = call(
        world,
        "jira_add_comment",
        arguments,
        context=world.context(profile, approval_id=approved.approval_id),
    )
    assert result["outcome"] == "success"
    assert len(world.provider.calls) == 1


def test_provider_errors_are_normalized_without_diagnostics() -> None:
    world = World()
    missing = call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "ALPHA-99"})
    assert missing["error"]["code"] == "RESOURCE_NOT_FOUND"
    for code in (
        JiraErrorCode.UPSTREAM_AUTH_CONFIGURATION,
        JiraErrorCode.UPSTREAM_TIMEOUT,
        JiraErrorCode.MALFORMED_UPSTREAM_RESPONSE,
        JiraErrorCode.TRANSIENT_UPSTREAM_FAILURE,
    ):
        world.provider.fail_next = code
        result = call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "ALPHA-1"})
        assert result["error"]["code"] == code.value
        assert "connection-initiative-alpha" not in str(result)
    world.provider.fail_next = JiraErrorCode.UPSTREAM_TIMEOUT
    write_timeout = call(
        world,
        "jira_create_issue",
        {"project_key": "ALPHA", "issue_type": "Task", "summary": "Retry risk"},
    )
    assert write_timeout["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert write_timeout["error"]["retryable"] is False
    cancelled = call(
        world,
        "jira_get_issue",
        {"project_key": "ALPHA", "issue_key": "ALPHA-1"},
        context=world.context(cancelled=True),
    )
    assert cancelled["error"]["code"] == "CANCELLED"
    assert len(world.provider.calls) == 6


def test_malformed_or_wrong_project_provider_response_fails_closed() -> None:
    class BadProvider(InMemoryJiraProvider):
        def get_issue(self, context, request):
            self._record("get_issue", context, request.project_key)
            return {"issue_key": "BETA-1", "project_key": "BETA", "summary": "leak"}

    world = World(provider=BadProvider())
    result = call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "ALPHA-1"})
    assert result["error"]["code"] == "MALFORMED_UPSTREAM_RESPONSE"

    class WrongSearchProvider(InMemoryJiraProvider):
        def search_issues(self, context, request):
            self._record("search_issues", context, request.project_key)
            return {
                "issues": [
                    issue("BETA-1").model_dump(
                        exclude={"kind", "description", "labels", "linked_issue_keys"}
                    )
                ],
                "has_more": False,
            }

    world = World(provider=WrongSearchProvider())
    result = call(world, "jira_search_issues", {"project_key": "ALPHA"})
    assert result["error"]["code"] == "MALFORMED_UPSTREAM_RESPONSE"

    class WrongProjectProvider(InMemoryJiraProvider):
        def get_issue(self, context, request):
            self._record("get_issue", context, request.project_key)
            return issue("BETA-1")

    world = World(provider=WrongProjectProvider())
    result = call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "ALPHA-1"})
    assert result["error"]["code"] == "MALFORMED_UPSTREAM_RESPONSE"


def test_binding_failure_and_strict_public_schema() -> None:
    world = World()
    definitions = world.mcp.definitions()
    assert len(definitions) == 6
    assert "jira_delete_issue" not in {definition["name"] for definition in definitions}
    schemas = str(definitions)
    for private_field in (
        "connection_alias",
        "site_url",
        "secret_arn",
        "token",
        "initiative_id",
        "allowed_projects",
        "environment",
        "is_admin",
    ):
        assert private_field not in schemas
    binding_key = ResourceBindingKey("dev", ALPHA.initiative.id, ResourceType.JIRA, "jira")
    world.bindings.disable(binding_key, expected_revision=1, correlation_id="test")
    result = call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "ALPHA-1"})
    assert result["error"]["code"] == "UPSTREAM_AUTH_CONFIGURATION"
    assert world.provider.calls == []
    for injected in (
        {"site_url": "https://example.invalid"},
        {"connection_alias": "other"},
        {"token": "secret"},
        {"initiative_id": BETA.initiative.id},
        {"allowed_projects": ["BETA"]},
        {"is_admin": True},
    ):
        args = {"project_key": "ALPHA", "issue_key": "ALPHA-1", **injected}
        denied = call(world, "jira_get_issue", args)
        assert denied["error"]["code"] == "INVALID_ARGUMENT"
    assert world.provider.calls == []
    with pytest.raises(KeyError):
        call(world, "jira_delete_issue", {"project_key": "ALPHA"})


def test_audit_event_contains_no_binding_or_business_body_and_failure_stops_result() -> None:
    world = World()
    call(
        world,
        "jira_add_comment",
        {"project_key": "ALPHA", "issue_key": "ALPHA-1", "body": "Sensitive body"},
    )
    event = world.audit.events[-1]
    assert event.outcome == "success" and event.policy_decision_id
    assert "Sensitive body" not in str(event)
    assert "connection-initiative-alpha" not in str(event)

    class FailingAudit:
        def record(self, event):
            raise RuntimeError("sink offline")

    world.service._audit = FailingAudit()
    with pytest.raises(JiraAuditError):
        call(world, "jira_get_issue", {"project_key": "ALPHA", "issue_key": "ALPHA-1"})
