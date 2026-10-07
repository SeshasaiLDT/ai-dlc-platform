"""ServiceNow governed boundary and offline provider regression tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.resource_bindings import (
    InMemoryBindingEventSink,
    InMemoryResourceBindingRepository,
)
from ai_dlc.adapters.servicenow import (
    InMemoryServiceNowProvider,
    InMemoryServiceNowToolAuditSink,
)
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink, InMemoryToolPolicyRepository
from ai_dlc.application.authorization import (
    AuthorizationService,
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
    resolve_authorization_context,
)
from ai_dlc.application.resource_bindings import (
    ResourceBindingKey,
    ResourceBindingRegistry,
    ResourceType,
    ServiceNowBinding,
    TrustedResolutionContext,
)
from ai_dlc.application.servicenow import (
    GovernedServiceNowService,
    RecordType,
    ServiceNowAuditError,
    ServiceNowErrorCode,
    ServiceNowMcpToolHandler,
    ServiceNowRecordDetail,
    TrustedServiceNowContext,
)
from ai_dlc.application.tool_policy import (
    ServiceNowOperation,
    ToolPolicy,
    ToolPolicyEffect,
    ToolPolicyService,
)
from ai_dlc.domain.identity import InitiativeMembership, Principal, Role, ToolPermission
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/initiatives/examples/travel-platform.yaml"
NOW = datetime(2026, 10, 7, tzinfo=UTC)
USER = Principal("shared-user", "test")
PERMISSIONS = frozenset({ToolPermission.SERVICENOW_READ, ToolPermission.SERVICENOW_WRITE})
OPERATIONS = (
    ServiceNowOperation.READ_RECORD,
    ServiceNowOperation.SEARCH,
    ServiceNowOperation.CREATE_RECORD,
    ServiceNowOperation.UPDATE_RECORD,
    ServiceNowOperation.ADD_COMMENT,
)


def make_profile(initiative: str, scope: str, *, groups=(), approval=False):
    document = load_initiative_profile(EXAMPLE).model_dump(mode="json")
    document["initiative"]["id"] = initiative
    document["integrations"]["servicenow"] = {
        "enabled": True,
        "scopes": [scope],
        "assignment_groups": list(groups),
    }
    document["policies"]["servicenow_write"] = {
        "enabled": True,
        "human_approval_required": approval,
    }
    return InitiativeProfile.model_validate(document)


ALPHA = make_profile("initiative-alpha", "incident")
BETA = make_profile("initiative-beta", "request")


def record(record_id: str, scope: str, *, group=None):
    return ServiceNowRecordDetail(
        record_id=record_id,
        number=f"{scope.upper()}-1",
        scope_id=scope,
        record_type=RecordType(scope),
        short_description="Example",
        state="open",
        assignment_group=group,
        updated_at=NOW,
        description="Sensitive description",
    )


class World:
    def __init__(
        self, *, profiles=(ALPHA, BETA), permissions=PERMISSIONS, policies=None, provider=None
    ):
        self.provider = provider or InMemoryServiceNowProvider(
            (record("record-a", "incident"), record("record-b", "request"))
        )
        self.audit = InMemoryServiceNowToolAuditSink()
        self.memberships = InMemoryMembershipRepository(
            tuple(
                InitiativeMembership(USER.subject_id, profile.initiative.id, (Role.DEVELOPER,))
                for profile in profiles
            )
        )
        self.roles = RolePolicy((RoleGrant(Role.DEVELOPER, tool_permissions=permissions),))
        authorization = AuthorizationService(
            self.memberships, self.roles, InMemoryAuthorizationAuditSink()
        )
        self.policy = ToolPolicyService(
            authorization,
            InMemoryToolPolicyRepository(
                policies
                if policies is not None
                else tuple(
                    ToolPolicy(profile.initiative.id, op, ToolPolicyEffect.ALLOW)
                    for profile in profiles
                    for op in OPERATIONS
                )
            ),
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
                ResourceBindingKey(
                    "dev", profile.initiative.id, ResourceType.SERVICENOW, "servicenow"
                ),
                ServiceNowBinding(
                    f"connection-{profile.initiative.id}",
                    f"instance-{profile.initiative.id}",
                ),
                correlation_id="setup",
            )
        self.mcp = ServiceNowMcpToolHandler(
            GovernedServiceNowService(self.policy, self.bindings, self.provider, self.audit)
        )

    def context(self, profile=ALPHA, *, restriction=None, cancelled=False):
        authorization = resolve_authorization_context(
            USER,
            profile.initiative.id,
            profile,
            self.memberships,
            self.roles,
            scope_restriction=restriction,
        )
        return TrustedServiceNowContext(
            TrustedResolutionContext("dev", profile, authorization, "trace-1"),
            initiative_revision=1,
            workspace_id="workspace-1",
            task_id="task-1",
            cancelled=cancelled,
        )


def call(world, tool, arguments, *, context=None):
    return world.mcp.invoke(tool, arguments, context=context or world.context())


def args(scope="incident", **other):
    return {"scope_id": scope, "record_type": scope, **other}


def test_get_uses_binding_and_normalizes_without_leaking_aliases():
    world = World()
    result = call(world, "servicenow_get_record", args(record_id="record-a"))
    assert result["outcome"] == "success"
    assert result["data"]["kind"] == "record_detail"
    assert result["data"]["record_id"] == "record-a"
    assert world.provider.calls[0].connection_alias == "connection-initiative-alpha"
    assert world.audit.events[0].principal_id == USER.subject_id
    assert "connection-initiative-alpha" not in str(result)
    assert "instance-initiative-alpha" not in str(result)


def test_scope_type_and_injected_fields_denied_before_provider():
    world = World()
    results = [
        call(world, "servicenow_get_record", args("request", record_id="record-b")),
        call(
            world,
            "servicenow_get_record",
            {"scope_id": "incident", "record_type": "request", "record_id": "record-a"},
        ),
        call(world, "servicenow_search_records", args(encoded_query="ORscope=request")),
        call(world, "servicenow_search_records", args(table="incident")),
        call(world, "servicenow_search_records", args(instance_url="https://example.invalid")),
        call(world, "servicenow_search_records", args(connection_alias="other")),
        call(
            world,
            "servicenow_search_records",
            args(),
            context=world.context(restriction=ScopeRestriction(servicenow_scopes=frozenset())),
        ),
    ]
    assert [item["error"]["code"] for item in results] == [
        "INVALID_SCOPE",
        "INVALID_ARGUMENT",
        "INVALID_ARGUMENT",
        "INVALID_ARGUMENT",
        "INVALID_ARGUMENT",
        "INVALID_ARGUMENT",
        "INVALID_SCOPE",
    ]
    assert world.provider.calls == []


def test_search_pagination_empty_and_multi_initiative():
    world = World(
        provider=InMemoryServiceNowProvider(
            (
                record("record-a", "incident"),
                record("record-c", "incident"),
                record("record-b", "request"),
            )
        )
    )
    first = call(world, "servicenow_search_records", args(page_size=1))
    second = call(
        world, "servicenow_search_records", args(page_size=1, cursor=first["data"]["next_cursor"])
    )
    assert [item["record_id"] for item in first["data"]["records"]] == ["record-a"]
    assert [item["record_id"] for item in second["data"]["records"]] == ["record-c"]
    assert second["data"]["has_more"] is False
    assert call(world, "servicenow_search_records", args(text="absent"))["data"]["records"] == []
    beta = call(
        world,
        "servicenow_get_record",
        args("request", record_id="record-b"),
        context=world.context(BETA),
    )
    wrong = call(
        world, "servicenow_get_record", args(record_id="record-a"), context=world.context(BETA)
    )
    assert beta["outcome"] == "success"
    assert wrong["error"]["code"] == "INVALID_SCOPE"


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("servicenow_create_record", args(short_description="New")),
        ("servicenow_update_record", args(record_id="record-a", state="closed")),
        (
            "servicenow_add_comment",
            args(record_id="record-a", channel="comment", body="Private text"),
        ),
    ],
)
def test_read_permission_cannot_write(tool, arguments):
    world = World(permissions=frozenset({ToolPermission.SERVICENOW_READ}))
    assert call(world, tool, arguments)["error"]["code"] == "PERMISSION_DENIED"
    assert world.provider.calls == []


def test_writes_are_scoped_and_journal_channels_are_bounded():
    world = World()
    assert (
        call(world, "servicenow_create_record", args("request", short_description="No"))["error"][
            "code"
        ]
        == "INVALID_SCOPE"
    )
    assert world.provider.calls == []
    created = call(world, "servicenow_create_record", args(short_description="New"))
    key = created["data"]["record_id"]
    updated = call(world, "servicenow_update_record", args(record_id=key, state="closed"))
    comment = call(
        world, "servicenow_add_comment", args(record_id=key, channel="comment", body="Public text")
    )
    note = call(
        world,
        "servicenow_add_comment",
        args(record_id=key, channel="work_note", body="Internal text"),
    )
    assert [item["outcome"] for item in (created, updated, comment, note)] == ["success"] * 4
    assert comment["data"]["channel"] == "comment" and note["data"]["channel"] == "work_note"
    bad = call(
        world, "servicenow_add_comment", args(record_id=key, channel="sys_journal_field", body="No")
    )
    assert bad["error"]["code"] == "INVALID_ARGUMENT"
    assert "Public text" not in str(world.audit.events)
    assert "Internal text" not in str(world.audit.events)


def test_exact_policy_denial_and_approval_gate_stop_provider():
    policies = tuple(
        ToolPolicy(ALPHA.initiative.id, op, ToolPolicyEffect.DENY) for op in OPERATIONS
    )
    world = World(profiles=(ALPHA,), policies=policies)
    assert (
        call(world, "servicenow_get_record", args(record_id="record-a"))["error"]["code"]
        == "PERMISSION_DENIED"
    )
    assert world.provider.calls == []
    approval_profile = make_profile("initiative-approval", "incident", approval=True)
    other = World(profiles=(approval_profile,))
    assert (
        call(
            other,
            "servicenow_create_record",
            args(short_description="New"),
            context=other.context(approval_profile),
        )["error"]["code"]
        == "PERMISSION_DENIED"
    )
    assert other.provider.calls == []


def test_wrong_provider_scope_type_identity_and_malformed_response_fail_closed():
    class BadProvider(InMemoryServiceNowProvider):
        def get_record(self, context, request):
            super().get_record(context, request)
            return record("record-b", "request")

    world = World(provider=BadProvider((record("record-a", "incident"),)))
    assert (
        call(world, "servicenow_get_record", args(record_id="record-a"))["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
    assert (
        call(world, "servicenow_update_record", args(record_id="record-a", state="closed"))[
            "error"
        ]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
    assert [item.operation for item in world.provider.calls] == ["get_record", "get_record"]

    class MalformedProvider(InMemoryServiceNowProvider):
        def search_records(self, context, request):
            self._record("search_records", context, request.scope_id)
            return {"kind": "search_page", "records": [{"record_id": "broken"}], "has_more": False}

    other = World(provider=MalformedProvider())
    assert (
        call(other, "servicenow_search_records", args())["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )

    class CrossScopeSearch(InMemoryServiceNowProvider):
        def search_records(self, context, request):
            self._record("search_records", context, request.scope_id)
            return {
                "kind": "search_page",
                "records": [
                    record("record-b", "request").model_dump(
                        exclude={"kind", "description", "requester_display"}
                    )
                ],
                "has_more": False,
            }

    cross = World(provider=CrossScopeSearch())
    assert (
        call(cross, "servicenow_search_records", args())["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )


def test_assignment_group_limits_reads_searches_and_mutations():
    scoped = make_profile("initiative-scoped", "incident", groups=("approved",))
    provider = InMemoryServiceNowProvider(
        (
            record("record-approved", "incident", group="approved"),
            record("record-other", "incident", group="other"),
        )
    )
    world = World(profiles=(scoped,), provider=provider)
    context = world.context(scoped)
    assert (
        call(world, "servicenow_get_record", args(record_id="record-approved"), context=context)[
            "outcome"
        ]
        == "success"
    )
    assert (
        call(world, "servicenow_get_record", args(record_id="record-other"), context=context)[
            "error"
        ]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
    before = len(provider.calls)
    assert (
        call(world, "servicenow_search_records", args(assignment_group="other"), context=context)[
            "error"
        ]["code"]
        == "INVALID_SCOPE"
    )
    assert (
        call(world, "servicenow_create_record", args(short_description="No"), context=context)[
            "error"
        ]["code"]
        == "INVALID_SCOPE"
    )
    assert len(provider.calls) == before
    assert (
        call(
            world,
            "servicenow_update_record",
            args(record_id="record-other", state="closed"),
            context=context,
        )["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
    assert provider.calls[-1].operation == "get_record"

    class LeakySearch(InMemoryServiceNowProvider):
        def search_records(self, context, request):
            self._record("search_records", context, request.scope_id)
            return {
                "kind": "search_page",
                "records": [
                    record("record-other", "incident", group="other").model_dump(
                        exclude={"kind", "description", "requester_display"}
                    )
                ],
                "has_more": False,
            }

    leaky = World(profiles=(scoped,), provider=LeakySearch())
    assert (
        call(leaky, "servicenow_search_records", args(), context=leaky.context(scoped))["error"][
            "code"
        ]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )


def test_upstream_errors_cancellation_and_not_found_are_normalized():
    world = World()
    assert (
        call(world, "servicenow_get_record", args(record_id="missing"))["error"]["code"]
        == "RESOURCE_NOT_FOUND"
    )
    world.provider.fail_next = ServiceNowErrorCode.UPSTREAM_TIMEOUT
    timeout = call(world, "servicenow_search_records", args())
    assert timeout["error"]["code"] == "UPSTREAM_TIMEOUT" and timeout["error"]["retryable"]
    world.provider.fail_next = ServiceNowErrorCode.UPSTREAM_TIMEOUT
    write = call(world, "servicenow_create_record", args(short_description="New"))
    assert write["error"]["code"] == "UPSTREAM_TIMEOUT" and not write["error"]["retryable"]
    before = len(world.provider.calls)
    assert (
        call(
            world,
            "servicenow_get_record",
            args(record_id="record-a"),
            context=world.context(cancelled=True),
        )["error"]["code"]
        == "CANCELLED"
    )
    assert len(world.provider.calls) == before
    for failure in (
        ServiceNowErrorCode.UPSTREAM_AUTH_CONFIGURATION,
        ServiceNowErrorCode.TRANSIENT_UPSTREAM_FAILURE,
        ServiceNowErrorCode.MALFORMED_UPSTREAM_RESPONSE,
    ):
        world.provider.fail_next = failure
        result = call(world, "servicenow_get_record", args(record_id="record-a"))
        assert result["error"]["code"] == failure.value


def test_public_schemas_are_business_only_and_delete_is_unregistered():
    definitions = ServiceNowMcpToolHandler.definitions()
    assert len(definitions) == 5
    assert "servicenow_delete_record" not in {item["name"] for item in definitions}
    schemas = str(definitions).lower()
    for forbidden in (
        "connection_alias",
        "instance_alias",
        "instance_url",
        "encoded_query",
        "table",
        "token",
        "password",
        "environment",
        "initiative_id",
    ):
        assert forbidden not in schemas


def test_mutated_provider_model_is_revalidated_and_audit_failure_blocks_result():
    class MutatedProvider(InMemoryServiceNowProvider):
        def get_record(self, context, request):
            item = super().get_record(context, request)
            return item.model_copy(update={"updated_at": datetime(2026, 10, 7)})

    world = World(provider=MutatedProvider((record("record-a", "incident"),)))
    assert (
        call(world, "servicenow_get_record", args(record_id="record-a"))["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )

    class BrokenAudit:
        def record(self, event):
            raise RuntimeError("audit unavailable")

    service = GovernedServiceNowService(world.policy, world.bindings, world.provider, BrokenAudit())
    with pytest.raises(ServiceNowAuditError):
        service.invoke(
            ServiceNowOperation.READ_RECORD,
            args(record_id="record-a"),
            context=world.context(),
        )
