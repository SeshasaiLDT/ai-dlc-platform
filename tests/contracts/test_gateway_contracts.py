"""Offline harness -> reference -> Lambda target -> governed service contracts."""

import json
from dataclasses import replace

import pytest
from support.enterprise import (
    ERROR_TYPES,
    LABELS,
    OTHER,
    PERMISSIONS,
    READ_PERMISSIONS,
    READ_TOOLS,
    SCOPE_FIELDS,
    SEARCH_TOOLS,
    WRITE_TOOLS,
    profile,
    read_arguments,
    search_arguments,
    write_arguments,
)

from ai_dlc.adapters.gateway import GatewayLambdaTarget, synthesize_gateway_template
from ai_dlc.application.authorization import RoleGrant, RolePolicy, ScopeRestriction
from ai_dlc.application.gateway import (
    INVOCATION_REF_FIELD,
    GatewayCatalog,
    GatewayContextResolver,
    UnknownGatewayToolError,
)
from ai_dlc.application.tool_policy import ToolPolicyEffect
from ai_dlc.domain.identity import Role, ToolPermission
from ai_dlc.domain.initiative import InitiativeProfile

pytestmark = pytest.mark.contract


def test_runtime_target_path_executes_real_governed_handler_with_hidden_routing(world, domain):
    result = world.runtime.invoke(
        READ_TOOLS[domain], read_arguments(domain), context=world.context()
    )
    assert result["outcome"] == "success"
    assert world.provider(domain).calls[-1].context.binding == world.details(domain)
    reference = world.transport.last_event[INVOCATION_REF_FIELD]
    assert reference not in str(result)
    assert reference not in repr(world.telemetry.events)
    assert "managed-" not in str(result)
    assert all(
        INVOCATION_REF_FIELD not in item["inputSchema"]["properties"]
        for item in world.runtime.available_tools(context=world.context())
    )
    assert world.audits[domain].events[-1].principal_id == "contract-user"


def test_hidden_write_manually_addressed_at_static_target_still_denied(world_factory, domain):
    world = world_factory(permissions=READ_PERMISSIONS)
    context = world.context()
    tool, arguments = WRITE_TOOLS[domain], write_arguments(domain)
    assert tool not in {item["name"] for item in world.runtime.available_tools(context=context)}
    issued = world.invocations.issue(tool, arguments, context=context, message_id="manual-message")
    result = world.transport.invoke(
        tool,
        {**arguments, INVOCATION_REF_FIELD: issued.reference},
        context=context,
        message_id="manual-message",
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert world.audits[domain].events[-1].outcome == "error"
    assert world.calls() == []


def test_reference_cannot_be_supplied_by_agent_or_reused_from_another_session(world, domain):
    other = world.context(1, principal=OTHER)
    tool = READ_TOOLS[domain]
    issued = world.invocations.issue(
        tool, read_arguments(domain, 1), context=other, message_id="other-session-message"
    )
    with pytest.raises(PermissionError, match="agent supplied"):
        world.runtime.invoke(
            tool,
            {**read_arguments(domain), INVOCATION_REF_FIELD: issued.reference},
            context=world.context(),
        )
    assert world.transport.last_event is None
    assert world.calls() == []


@pytest.mark.parametrize(
    "mismatch",
    [
        "missing",
        "unknown",
        "gateway",
        "target",
        "tool",
        "message",
        "arguments",
        "version",
        "missing_version",
    ],
)
def test_reference_or_metadata_mismatch_is_rejected_before_provider(world, domain, mismatch):
    def mutate(event, metadata):
        if mismatch == "missing":
            event.pop(INVOCATION_REF_FIELD)
        elif mismatch == "unknown":
            event[INVOCATION_REF_FIELD] = "x" * 43
        elif mismatch == "arguments":
            event.update(read_arguments(domain, 1))
        elif mismatch == "missing_version":
            metadata.pop("bedrockAgentCoreMessageVersion")
        else:
            key, value = {
                "gateway": ("bedrockAgentCoreGatewayId", "other-gateway"),
                "target": ("bedrockAgentCoreTargetId", "other-target"),
                "tool": ("bedrockAgentCoreToolName", f"{LABELS[domain]}___{SEARCH_TOOLS[domain]}"),
                "message": ("bedrockAgentCoreMcpMessageId", "other-message"),
                "version": ("bedrockAgentCoreMessageVersion", "2.0"),
            }[mismatch]
            metadata[key] = value

    world.transport.mutate = mutate
    with pytest.raises(PermissionError):
        world.runtime.invoke(READ_TOOLS[domain], read_arguments(domain), context=world.context())
    assert not world.calls()
    assert not world.audits[domain].events


def test_reference_replay_cannot_repeat_provider_execution(world, domain):
    world.runtime.invoke(READ_TOOLS[domain], read_arguments(domain), context=world.context())
    with pytest.raises(PermissionError, match="unavailable"):
        world.transport.deliver(
            LABELS[domain], world.transport.last_event, world.transport.last_metadata
        )
    assert len(world.provider(domain).calls) == 1


def test_expiry_is_checked_even_when_record_has_not_been_deleted_by_ttl(world, domain, monkeypatch):
    import ai_dlc.application.gateway.invocation as invocation

    def expire(event, metadata):
        monkeypatch.setattr(invocation, "time", lambda: 10**12)

    world.transport.mutate = expire
    with pytest.raises(PermissionError, match="mismatch"):
        world.runtime.invoke(READ_TOOLS[domain], read_arguments(domain), context=world.context())
    assert not world.calls()


@pytest.mark.parametrize("changed", ["user", "initiative", "revision", "broadened_permissions"])
def test_context_reresolution_cannot_change_issued_authority(world, domain, changed, monkeypatch):
    context = world.context()
    if changed in ("user", "initiative"):
        replacement = world.context(principal=OTHER) if changed == "user" else world.context(1)
        monkeypatch.setattr(world.resolver, "resolve", lambda selection: replacement)
    elif changed == "revision":
        world.transport.mutate = lambda event, metadata: world.initiatives.update(
            world.profiles[0].initiative.id, world.profiles[0]
        )
    else:
        expanded = RolePolicy(
            (
                RoleGrant(
                    Role.DEVELOPER, tool_permissions=PERMISSIONS | {ToolPermission.ARTIFACT_READ}
                ),
            )
        )
        resolver = GatewayContextResolver(world.initiatives, world.memberships, expanded)
        label = LABELS[domain]
        world.targets[label] = GatewayLambdaTarget(
            world.router, world.invocation_registry(resolver=resolver), target_name=label
        )
    with pytest.raises(PermissionError, match="authorization changed"):
        world.runtime.invoke(READ_TOOLS[domain], read_arguments(domain), context=context)
    assert not world.calls()


def test_narrowed_scopes_approvals_and_task_context_survive_correlation(world, domain):
    scope = world.context().resolution.authorization.allowed_scopes
    restriction = ScopeRestriction(**{SCOPE_FIELDS[domain]: getattr(scope, SCOPE_FIELDS[domain])})
    context = world.context(scope_restriction=restriction, approval_id="server-approval")
    result = world.runtime.invoke(READ_TOOLS[domain], read_arguments(domain), context=context)
    assert result["outcome"] == "success"
    audit = world.audits[domain].events[-1]
    assert (audit.workspace_id, audit.task_id) == ("workspace-one", "task-one")
    reference = world.transport.last_event[INVOCATION_REF_FIELD]
    for name in ("approval_id", "principal", "initiative_id", "allowed_scopes"):
        assert name not in world.transport.last_event
    assert reference not in repr(world.telemetry.events)


def test_correlation_retains_narrowing_when_profile_has_additional_resources(
    world_factory, domain, monkeypatch
):
    first = profile(0).model_dump(mode="json")
    first["integrations"]["jira"]["projects"].append("BETA")
    first["integrations"]["servicenow"]["scopes"].append("request")
    first["integrations"]["git"]["repositories"].append(
        {
            **first["integrations"]["git"]["repositories"][0],
            "id": "repo-extra",
        }
    )
    world = world_factory(profiles=(InitiativeProfile.model_validate(first), profile(1)))
    allowed = {
        "jira": frozenset({"ALPHA"}),
        "servicenow": frozenset({"incident"}),
        "git": frozenset({"repo-a"}),
    }[domain]
    context = world.context(scope_restriction=ScopeRestriction(**{SCOPE_FIELDS[domain]: allowed}))
    captured = []
    original = world.router.invoke

    def invoke(tool_name, arguments, *, context):
        captured.append(context)
        return original(tool_name, arguments, context=context)

    monkeypatch.setattr(world.router, "invoke", invoke)
    assert (
        world.runtime.invoke(READ_TOOLS[domain], read_arguments(domain), context=context)["outcome"]
        == "success"
    )
    authorization = captured[0].resolution.authorization
    assert getattr(authorization.allowed_scopes, SCOPE_FIELDS[domain]) == allowed
    assert len(getattr(authorization.configured_scopes, SCOPE_FIELDS[domain])) == 2


def test_model_tool_schemas_contain_only_business_arguments(world):
    forbidden = {
        "principal",
        "initiative_id",
        "environment",
        "allowed_scopes",
        "is_admin",
        "connection_alias",
        "secret_arn",
        "token",
        "site_url",
        "instance_url",
        "table",
        "remote_url",
        "owner",
        "provider",
        "approved_commits",
        INVOCATION_REF_FIELD,
    }
    for tool in world.catalog.tools:
        properties = set(tool.input_schema["properties"])
        assert not properties & forbidden, tool.name


def test_cancellation_survives_full_gateway_path(world, domain):
    result = world.runtime.invoke(
        READ_TOOLS[domain], read_arguments(domain), context=world.context(cancelled=True)
    )
    assert result["error"]["code"] == "CANCELLED"
    assert not world.calls()


@pytest.mark.parametrize("code", ["UPSTREAM_TIMEOUT", "MALFORMED_UPSTREAM_RESPONSE"])
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
def test_gateway_preserves_normalized_provider_failure_and_write_retryability(
    world, domain, code, write
):
    world.provider(domain).backend.fail_next = ERROR_TYPES[domain](code)
    result = world.runtime.invoke(
        WRITE_TOOLS[domain] if write else READ_TOOLS[domain],
        write_arguments(domain) if write else read_arguments(domain),
        context=world.context(),
    )
    assert result["error"]["code"] == code
    assert result["error"]["retryable"] == (not write and code == "UPSTREAM_TIMEOUT")
    assert "data" not in result
    assert len(world.provider(domain).calls) == 1


def test_discovery_filters_read_write_without_executing_policy_or_providers(world_factory, domain):
    for permissions in (frozenset(), READ_PERMISSIONS, PERMISSIONS):
        world = world_factory(permissions=permissions)
        visible = {item["name"] for item in world.runtime.available_tools(context=world.context())}
        assert (READ_TOOLS[domain] in visible) == (permissions != frozenset())
        assert (WRITE_TOOLS[domain] in visible) == (permissions == PERMISSIONS)
        assert not world.calls()
        assert not world.audits[domain].events


def test_policy_deny_hides_tool_and_approval_is_advertised_without_granting_it(
    world_factory, domain
):
    base = world_factory()
    read_op = base.catalog.by_name[READ_TOOLS[domain]].operation
    write_op = base.catalog.by_name[WRITE_TOOLS[domain]].operation
    world = world_factory(
        policy_effects={
            read_op: ToolPolicyEffect.DENY,
            write_op: ToolPolicyEffect.REQUIRE_APPROVAL,
        }
    )
    visible = {
        item["name"]: item for item in world.runtime.available_tools(context=world.context())
    }
    assert READ_TOOLS[domain] not in visible
    assert visible[WRITE_TOOLS[domain]]["approvalRequired"] is True
    denied = world.runtime.invoke(
        WRITE_TOOLS[domain], write_arguments(domain), context=world.context()
    )
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert not world.calls()


def test_selected_initiative_changes_discovery_without_user_tool_mappings(world_factory):
    documents = [profile(index).model_dump(mode="json") for index in (0, 1)]
    documents[0]["integrations"]["servicenow"] = {
        "enabled": False,
        "scopes": [],
        "assignment_groups": [],
    }
    documents[0]["integrations"]["git"]["repositories"][0]["access"] = "read_only"
    documents[1]["integrations"]["jira"] = {"enabled": False, "projects": []}
    world = world_factory(
        profiles=tuple(InitiativeProfile.model_validate(doc) for doc in documents)
    )
    first = {item["name"] for item in world.runtime.available_tools(context=world.context(0))}
    second = {item["name"] for item in world.runtime.available_tools(context=world.context(1))}
    assert READ_TOOLS["jira"] in first and READ_TOOLS["jira"] not in second
    assert READ_TOOLS["servicenow"] not in first and READ_TOOLS["servicenow"] in second
    assert WRITE_TOOLS["git"] not in first and WRITE_TOOLS["git"] in second


def projected_shape_accepts(schema, value):
    expected = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}
    if not isinstance(value, expected[schema["Type"]]):
        return False
    if schema["Type"] == "object":
        return set(schema.get("Required", ())) <= set(value) and all(
            name not in value or projected_shape_accepts(child, value[name])
            for name, child in schema.get("Properties", {}).items()
        )
    if schema["Type"] == "array":
        return all(projected_shape_accepts(schema["Items"], item) for item in value)
    return True


def test_projected_gateway_shape_never_replaces_strict_handler_validation(world, domain):
    tool = SEARCH_TOOLS[domain]
    arguments = {**search_arguments(domain), "page_size": 101}
    if domain == "servicenow":
        arguments["record_type"] = "arbitrary_table"
    elif domain == "git":
        tool = "git_remote_get_branch"
        arguments = {"repository_id": "repo-a", "branch": "../outside"}
    schema = next(
        item["InputSchema"]
        for item in synthesize_gateway_template(world.catalog)["Resources"][
            f"{LABELS[domain]}Target"
        ]["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
        if item["Name"] == tool
    )
    assert projected_shape_accepts(
        schema, {**arguments, INVOCATION_REF_FIELD: "reference-placeholder"}
    )
    result = world.runtime.invoke(tool, arguments, context=world.context())
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert not world.calls()


def test_catalog_is_deterministic_unique_and_excludes_local_or_deleted_operations(world):
    names = tuple(tool.name for tool in world.catalog.tools)
    assert names == tuple(sorted(names))
    assert len(names) == len(set(names)) == 20
    assert not any("workspace" in name or "delete" in name for name in names)
    assert world.catalog.version == GatewayCatalog.from_handlers().version
    tool = world.catalog.tools[0]
    changed = replace(tool, input_schema={**tool.input_schema, "description": "changed schema"})
    assert GatewayCatalog((changed, *world.catalog.tools[1:])).version != world.catalog.version
    with pytest.raises(ValueError, match="duplicate"):
        GatewayCatalog((tool, tool))


@pytest.mark.parametrize(
    "tool",
    [
        "unknown",
        "jira_delete_issue",
        "servicenow_delete_record",
        "git_remote_delete_branch",
        "workspace_commit",
        "__import__",
    ],
)
def test_unknown_local_and_unregistered_tools_fail_closed(world, tool):
    with pytest.raises(UnknownGatewayToolError):
        world.router.invoke(tool, {}, context=world.context())
    with pytest.raises(UnknownGatewayToolError):
        world.runtime.invoke(tool, {}, context=world.context())
    assert not world.calls()


def test_gateway_telemetry_failure_blocks_success(world, domain):
    world.telemetry.fail = True
    with pytest.raises(RuntimeError):
        world.runtime.invoke(READ_TOOLS[domain], read_arguments(domain), context=world.context())
    assert len(world.provider(domain).calls) == 1


def test_iac_encodes_only_expected_iam_and_same_account_targets(world):
    template = synthesize_gateway_template(world.catalog)
    resources = template["Resources"]
    gateway = resources["EnterpriseGateway"]["Properties"]
    assert resources["EnterpriseGateway"]["Type"] == "AWS::BedrockAgentCore::Gateway"
    assert (gateway["AuthorizerType"], gateway["ProtocolType"]) == ("AWS_IAM", "MCP")
    gateway_role = resources["GatewayRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    assert len(gateway_role) == 1 and gateway_role[0]["Action"] == ["lambda:InvokeFunction"]
    assert len(gateway_role[0]["Resource"]) == 3
    assert resources["GatewayRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0][
        "Condition"
    ]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
    expected = {
        "RuntimeGatewayInvokePolicy": (
            ["bedrock-agentcore:InvokeGateway"],
            [{"Fn::GetAtt": ["EnterpriseGateway", "GatewayArn"]}],
        ),
        "RuntimeInvocationWritePolicy": (
            ["dynamodb:PutItem"],
            [{"Fn::GetAtt": ["InvocationTable", "Arn"]}],
        ),
        **{
            f"{label}InvocationReadPolicy": (
                ["dynamodb:DeleteItem"],
                [{"Fn::GetAtt": ["InvocationTable", "Arn"]}],
            )
            for label in LABELS.values()
        },
    }
    assert {name for name, item in resources.items() if item["Type"] == "AWS::IAM::Policy"} == set(
        expected
    )
    for name, (actions, targets) in expected.items():
        statements = resources[name]["Properties"]["PolicyDocument"]["Statement"]
        assert statements == [{"Effect": "Allow", "Action": actions, "Resource": targets}]
    target_arns = []
    for label, parameter in (
        ("Jira", "JiraTargetFunctionName"),
        ("ServiceNow", "ServiceNowTargetFunctionName"),
        ("RemoteGit", "GitTargetFunctionName"),
    ):
        target = resources[f"{label}Target"]["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]
        arn = {
            "Fn::Sub": "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${"
            + parameter
            + "}"
        }
        assert target["LambdaArn"] == arn
        target_arns.append(arn)
        assert template["Parameters"][parameter]["AllowedPattern"] == "[a-zA-Z0-9_-]{1,64}"
        published = {
            item["Name"]: item["InputSchema"] for item in target["ToolSchema"]["InlinePayload"]
        }
        for name, schema in published.items():
            source = world.catalog.by_name[name].input_schema
            assert set(schema["Properties"]) == set(source["properties"]) | {INVOCATION_REF_FIELD}
            assert schema["Required"] == [*source.get("required", ()), INVOCATION_REF_FIELD]
    assert gateway_role[0]["Resource"] == target_arns
    table = resources["InvocationTable"]["Properties"]
    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}
    assert table["SSESpecification"] == {"SSEEnabled": True}
    assert set(template["Outputs"]) == {
        "GatewayArn",
        "GatewayId",
        "JiraTargetId",
        "ServiceNowTargetId",
        "RemoteGitTargetId",
        "Environment",
        "CatalogVersion",
        "InvocationTableName",
    }
    serialized = json.dumps(template)
    for forbidden in ("https://", "secret_arn", "connection_alias", "physical-repository", "token"):
        assert forbidden not in serialized.lower()
