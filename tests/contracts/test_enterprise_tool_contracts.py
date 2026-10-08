"""One observable trust/error contract exercised against three real governed services."""

import json

import pytest
from support.enterprise import (
    APPROVER,
    BUSINESS_CANARY,
    DOMAINS,
    ERROR_TYPES,
    PROVIDER_CANARY,
    READ_METHODS,
    READ_PERMISSIONS,
    READ_TOOLS,
    SCOPE_FIELDS,
    SEARCH_TOOLS,
    USER,
    WRITE_TOOLS,
    read_arguments,
    search_arguments,
    write_arguments,
    write_requests,
)

from ai_dlc.application.authorization import JiraProjectTarget, ScopeRestriction
from ai_dlc.application.git_remote import GitAuditError, RepositoryInfo
from ai_dlc.application.jira import JiraAuditError, JiraIssueDetail
from ai_dlc.application.servicenow import ServiceNowAuditError, ServiceNowRecordDetail
from ai_dlc.application.tool_policy import (
    GitTarget,
    ServiceNowTarget,
    ToolPolicyEffect,
    ToolPolicyRequest,
)
from ai_dlc.domain.identity import MembershipNotFoundError, Principal, ToolPermission
from ai_dlc.domain.initiative.enums import GitProvider

pytestmark = pytest.mark.contract


def assert_envelope(result, outcome):
    assert result["contract_version"] == 1
    assert result["outcome"] == outcome
    assert ("data" in result) != ("error" in result)
    assert result["correlation_id"] == "contract-trace"
    assert result["audit_ref"]
    text = json.dumps(result)
    for hidden in ("managed-", "physical-owner-", "physical-repository-", PROVIDER_CANARY):
        assert hidden not in text
    return result.get("error", {})


def test_happy_read_search_and_write_share_normalized_binding_contract(world, domain):
    for tool, arguments in (
        (READ_TOOLS[domain], read_arguments(domain)),
        (SEARCH_TOOLS[domain], search_arguments(domain)),
        (WRITE_TOOLS[domain], write_arguments(domain)),
    ):
        result = world.call(domain, tool, arguments)
        assert_envelope(result, "success")
        data = result["data"]
        records = data.get("issues", data.get("records", [data]))
        identity_field = {
            "jira": "project_key",
            "servicenow": "scope_id",
            "git": "repository_id",
        }[domain]
        assert all(
            item[identity_field] == search_arguments(domain)[identity_field] for item in records
        )
        call = world.provider(domain).calls[-1]
        assert call.context.binding == world.details(domain)
        assert call.context.correlation_id == "contract-trace"
        assert call.context.timeout_seconds == 10
        assert world.binding_repository.lookups[-1] == world.key(domain)
    assert not world.provider("git", 1).calls


WRITE_CASES = tuple(
    (domain, tool, arguments) for domain in DOMAINS for tool, arguments in write_requests(domain)
)


@pytest.mark.parametrize("selected,tool,arguments", WRITE_CASES, ids=[c[1] for c in WRITE_CASES])
def test_approved_write_surface_returns_normalized_results(world, selected, tool, arguments):
    context = world.context(approved_commits=frozenset({("repo-a", "b" * 40)}))
    result = world.call(selected, tool, arguments, context=context)
    assert_envelope(result, "success")
    assert world.provider(selected).calls[-1].context.binding == world.details(selected)


@pytest.mark.parametrize("selected,tool,arguments", WRITE_CASES, ids=[c[1] for c in WRITE_CASES])
def test_read_permission_cannot_authorize_any_write(world_factory, selected, tool, arguments):
    world = world_factory(permissions=READ_PERMISSIONS)
    context = world.context(approved_commits=frozenset({("repo-a", "b" * 40)}))
    result = world.call(selected, tool, arguments, context=context)
    assert assert_envelope(result, "error")["code"] == "PERMISSION_DENIED"
    assert world.calls() == []
    assert world.binding_repository.lookups == []


def test_write_permission_does_not_imply_read_permission(world_factory, domain):
    world = world_factory(permissions=frozenset({ToolPermission(f"{domain}.write")}))
    result = world.call(domain)
    assert assert_envelope(result, "error")["code"] == "PERMISSION_DENIED"
    assert not world.calls()


def test_narrowed_scope_denies_before_binding_and_provider(world, domain):
    restriction = ScopeRestriction(**{SCOPE_FIELDS[domain]: frozenset()})
    result = world.call(domain, context=world.context(scope_restriction=restriction))
    assert assert_envelope(result, "error")["code"] == "INVALID_SCOPE"
    assert not world.calls()
    assert world.binding_repository.lookups == []


def test_same_principal_switching_initiative_never_shares_logical_access(world, domain):
    for selected in (0, 1):
        context = world.context(selected)
        allowed = world.call(domain, arguments=read_arguments(domain, selected), context=context)
        assert_envelope(allowed, "success")
        before = len(world.calls())
        denied = world.call(domain, arguments=read_arguments(domain, 1 - selected), context=context)
        assert assert_envelope(denied, "error")["code"] == "INVALID_SCOPE"
        assert len(world.calls()) == before
        assert world.provider(domain, selected).calls[-1].context.binding == world.details(
            domain, selected
        )


def test_non_member_cannot_construct_trusted_context(world, domain):
    with pytest.raises(MembershipNotFoundError):
        world.context(principal=Principal("unassigned-user", "test"))
    assert not world.calls()


def test_inactive_initiative_cannot_construct_trusted_context(world, domain):
    world.initiatives.disable(world.profiles[0].initiative.id)
    with pytest.raises(ValueError, match="not active"):
        world.context()
    assert not world.calls()


def test_exact_operation_deny_stops_execution_despite_base_permission(world_factory, domain):
    catalog_world = world_factory()
    op = catalog_world.catalog.by_name[WRITE_TOOLS[domain]].operation
    world = world_factory(policy_effects={op: ToolPolicyEffect.DENY})
    denied = world.call(domain, WRITE_TOOLS[domain], write_arguments(domain))
    assert assert_envelope(denied, "error")["code"] == "PERMISSION_DENIED"
    assert not world.calls()
    assert world.binding_repository.lookups == []


def test_approval_is_required_and_bound_to_selected_operation_and_initiative(world_factory, domain):
    catalog_world = world_factory()
    op = catalog_world.catalog.by_name[WRITE_TOOLS[domain]].operation
    world = world_factory(policy_effects={op: ToolPolicyEffect.REQUIRE_APPROVAL})
    args = write_arguments(domain)
    denied = world.call(domain, WRITE_TOOLS[domain], args)
    assert assert_envelope(denied, "error")["code"] == "PERMISSION_DENIED"
    assert not world.calls()
    target = {
        "jira": JiraProjectTarget("ALPHA"),
        "servicenow": ServiceNowTarget("incident"),
        "git": GitTarget("repo-a", "feature/new"),
    }[domain]
    request = ToolPolicyRequest(USER, world.profiles[0].initiative.id, op, target)
    approval = world.approvals.request_approval(
        request, profile=world.profiles[0], request_key="contract-approval", initiative_revision=1
    )
    world.approvals.approve(
        approval.approval_id, APPROVER, profile=world.profiles[0], expected_version=approval.version
    )
    context = world.context(approval_id=approval.approval_id)
    result = world.runtime.invoke(WRITE_TOOLS[domain], args, context=context)
    assert_envelope(result, "success")
    before = len(world.calls())
    wrong = world.call(
        domain,
        WRITE_TOOLS[domain],
        write_arguments(domain, 1),
        context=world.context(1, approval_id=approval.approval_id),
    )
    assert assert_envelope(wrong, "error")["code"] == "PERMISSION_DENIED"
    assert len(world.calls()) == before


@pytest.mark.parametrize(
    "code",
    [
        "RESOURCE_NOT_FOUND",
        "UPSTREAM_AUTH_CONFIGURATION",
        "UPSTREAM_TIMEOUT",
        "MALFORMED_UPSTREAM_RESPONSE",
        "TRANSIENT_UPSTREAM_FAILURE",
    ],
)
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
def test_upstream_errors_are_safe_and_only_ambiguous_reads_are_retryable(
    world, domain, code, write
):
    world.provider(domain).backend.fail_next = ERROR_TYPES[domain](code)
    result = world.call(
        domain,
        WRITE_TOOLS[domain] if write else READ_TOOLS[domain],
        write_arguments(domain) if write else read_arguments(domain),
    )
    error = assert_envelope(result, "error")
    assert error["code"] == code
    assert error["retryable"] == (
        not write and code in {"UPSTREAM_TIMEOUT", "TRANSIENT_UPSTREAM_FAILURE"}
    )
    assert len(world.provider(domain).calls) == 1, "no automatic retry is allowed"


@pytest.mark.parametrize(
    "exception,code",
    [
        (TimeoutError, "UPSTREAM_TIMEOUT"),
        (RuntimeError, "TRANSIENT_UPSTREAM_FAILURE"),
    ],
)
def test_raw_provider_exception_details_never_reach_agents(world, domain, exception, code):
    def fail(context, request):
        raise exception(PROVIDER_CANARY)

    world.provider(domain).overrides[READ_METHODS[domain]] = fail
    result = world.call(domain)
    assert assert_envelope(result, "error")["code"] == code
    assert PROVIDER_CANARY not in repr(world.audits[domain].events)


@pytest.mark.parametrize("corruption", ["malformed", "cross_scope", "unchecked_model"])
def test_provider_data_is_revalidated_even_when_already_a_normalized_model(
    world, domain, corruption
):
    data = world.call(domain)["data"]
    world.provider(domain).calls.clear()
    if corruption == "cross_scope":
        if domain == "jira":
            data.update(project_key="BETA", issue_key="BETA-1")
        elif domain == "servicenow":
            data.update(scope_id="request", record_type="request")
        else:
            data["repository_id"] = "repo-b"
        raw = data
    elif corruption == "malformed":
        raw = {"raw_provider_payload": PROVIDER_CANARY}
    else:
        model_type = {
            "jira": JiraIssueDetail,
            "servicenow": ServiceNowRecordDetail,
            "git": RepositoryInfo,
        }[domain]
        field = {"jira": "summary", "servicenow": "short_description", "git": "display_name"}[
            domain
        ]
        raw = model_type.model_validate(data).model_copy(update={field: ""})
    world.provider(domain).overrides[READ_METHODS[domain]] = lambda context, request: raw
    result = world.call(domain)
    assert assert_envelope(result, "error")["code"] == "MALFORMED_UPSTREAM_RESPONSE"
    assert PROVIDER_CANARY not in json.dumps(result)


def test_provider_search_cannot_return_another_scope(world, domain):
    data = world.call(domain, SEARCH_TOOLS[domain], search_arguments(domain))["data"]
    if domain == "jira":
        data["issues"][0].update(project_key="BETA", issue_key="BETA-1")
        method = "search_issues"
    elif domain == "servicenow":
        data["records"][0].update(scope_id="request", record_type="request")
        method = "search_records"
    else:
        data["branches"][0]["repository_id"] = "repo-b"
        method = "list_branches"
    world.provider(domain).overrides[method] = lambda context, request: data
    result = world.call(domain, SEARCH_TOOLS[domain], search_arguments(domain))
    assert assert_envelope(result, "error")["code"] == "MALFORMED_UPSTREAM_RESPONSE"
    assert "data" not in result


@pytest.mark.parametrize(
    "field",
    [
        "principal",
        "initiative_id",
        "environment",
        "allowed_scopes",
        "is_admin",
        "connection_alias",
        "token",
        "site_url",
        "instance_url",
        "table",
        "remote_url",
    ],
)
def test_agent_arguments_cannot_inject_trust_or_physical_routing(world, domain, field):
    result = world.call(domain, arguments={**read_arguments(domain), field: PROVIDER_CANARY})
    assert assert_envelope(result, "error")["code"] == "INVALID_ARGUMENT"
    assert not world.calls()


def test_trusted_cancellation_prevents_execution(world, domain):
    result = world.call(domain, context=world.context(cancelled=True))
    assert assert_envelope(result, "error")["code"] == "CANCELLED"
    assert not world.calls()
    assert world.binding_repository.lookups == []


def test_audit_failure_blocks_success_after_provider_execution(world, domain):
    world.audits[domain].fail = True
    audit_error = {
        "jira": JiraAuditError,
        "servicenow": ServiceNowAuditError,
        "git": GitAuditError,
    }[domain]
    with pytest.raises(audit_error):
        world.call(domain)
    assert len(world.provider(domain).calls) == 1


def test_business_payloads_and_routing_do_not_enter_audit(world, domain):
    tool, args = {
        "jira": ("jira_add_comment", {**read_arguments("jira"), "body": BUSINESS_CANARY}),
        "servicenow": (
            "servicenow_add_comment",
            {
                **read_arguments("servicenow"),
                "channel": "work_note",
                "body": BUSINESS_CANARY,
            },
        ),
        "git": (
            "git_remote_create_pull_request",
            {
                **read_arguments("git"),
                "base_branch": "main",
                "head_branch": "feature/one",
                "title": "Safe title",
                "description": BUSINESS_CANARY,
            },
        ),
    }[domain]
    assert_envelope(world.call(domain, tool, args), "success")
    if domain == "git":
        assert_envelope(
            world.call(
                domain,
                "git_remote_get_diff",
                {
                    **read_arguments(domain),
                    "base_ref": "main",
                    "head_ref": "feature/one",
                },
            ),
            "success",
        )
    events = repr(world.audits[domain].events) + repr(world.binding_events.events)
    for hidden in (BUSINESS_CANARY, PROVIDER_CANARY, "managed-", "physical-repository-"):
        assert hidden not in events


def test_remote_git_branch_pr_and_bounded_diff_contract(world):
    for tool, arguments in (
        ("git_remote_get_branch", {"repository_id": "repo-a", "branch": "main"}),
        ("git_remote_get_pull_request", {"repository_id": "repo-a", "number": 1}),
        (
            "git_remote_get_diff",
            {
                "repository_id": "repo-a",
                "base_ref": "main",
                "head_ref": "feature/one",
            },
        ),
    ):
        result = world.call("git", tool, arguments)
        assert_envelope(result, "success")
    diff = result["data"]
    assert len(diff["files"]) <= 100
    assert sum(len((item.get("patch") or "").encode()) for item in diff["files"]) <= 32768
    assert diff["kind"] == "remote_diff"
    assert world.git[GitProvider.BITBUCKET].calls == []


@pytest.mark.parametrize(
    "tool,arguments,method,field,value",
    [
        (
            "git_remote_get_branch",
            {"repository_id": "repo-a", "branch": "main"},
            "get_branch",
            "name",
            "other-branch",
        ),
        (
            "git_remote_get_pull_request",
            {"repository_id": "repo-a", "number": 1},
            "get_pull_request",
            "number",
            999,
        ),
        (
            "git_remote_get_diff",
            {
                "repository_id": "repo-a",
                "base_ref": "main",
                "head_ref": "feature/one",
            },
            "get_diff",
            "head_ref",
            "other-branch",
        ),
        (
            "git_remote_create_pull_request",
            {
                "repository_id": "repo-a",
                "base_branch": "main",
                "head_branch": "feature/one",
                "title": "New",
            },
            "create_pull_request",
            "base_branch",
            "other-branch",
        ),
    ],
)
def test_git_provider_cannot_substitute_branch_pr_or_diff_identity(
    world, tool, arguments, method, field, value
):
    data = world.call("git", tool, arguments)["data"]
    data[field] = value
    world.provider("git").overrides[method] = lambda context, request: data
    result = world.call("git", tool, arguments)
    assert result["error"]["code"] == "MALFORMED_UPSTREAM_RESPONSE"
    assert "data" not in result


@pytest.mark.parametrize("oversized", ["file_patch", "file_count", "total_patch", "invalid_path"])
def test_remote_diff_payload_limits_fail_closed(world, oversized):
    arguments = {"repository_id": "repo-a", "base_ref": "main", "head_ref": "feature/one"}
    data = world.call("git", "git_remote_get_diff", arguments)["data"]
    if oversized == "file_patch":
        data["files"][0]["patch"] = "x" * 4097
    elif oversized == "invalid_path":
        data["files"][0]["path"] = "../outside"
    else:
        count = 101 if oversized == "file_count" else 9
        data["files"] = [
            {**data["files"][0], "path": f"file-{index}.py", "patch": "x" * 4096}
            for index in range(count)
        ]
        data["changed_file_count"] = count
    world.provider("git").overrides["get_diff"] = lambda context, request: data
    result = world.call("git", "git_remote_get_diff", arguments)
    assert result["error"]["code"] == "MALFORMED_UPSTREAM_RESPONSE"
    assert "data" not in result


def test_servicenow_returned_type_and_journal_channel_must_match_request(world):
    data = world.call("servicenow")["data"]
    data["record_type"] = "request"
    world.servicenow.overrides["get_record"] = lambda context, request: data
    assert world.call("servicenow")["error"]["code"] == "MALFORMED_UPSTREAM_RESPONSE"
    world.servicenow.overrides.clear()
    args = {**read_arguments("servicenow"), "channel": "work_note", "body": BUSINESS_CANARY}
    journal = world.call("servicenow", "servicenow_add_comment", args)["data"]
    journal["channel"] = "comment"
    world.servicenow.overrides["add_comment"] = lambda context, request: journal
    assert (
        world.call("servicenow", "servicenow_add_comment", args)["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
