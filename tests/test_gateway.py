"""AgentCore catalog, trusted discovery, dispatch and CloudFormation drift checks."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_dlc.adapters.authorization import InMemoryMembershipRepository
from ai_dlc.adapters.gateway import (
    DynamoDbInvocationRecordStore,
    GatewayLambdaTarget,
    InMemoryInvocationRecordStore,
    synthesize_gateway_template,
)
from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyRepository
from ai_dlc.application.authorization import RoleGrant, RolePolicy, ScopeRestriction
from ai_dlc.application.gateway import (
    INVOCATION_REF_FIELD,
    AuthenticatedRuntimeSelection,
    GatewayCatalog,
    GatewayContextResolver,
    GatewayDiscovery,
    GatewayRouter,
    GatewayRuntimeAccess,
    TrustedInvocationRegistry,
    UnknownGatewayToolError,
)
from ai_dlc.application.initiatives import InitiativeNotFoundError, InitiativeRegistry
from ai_dlc.application.tool_policy import (
    GitOperation,
    JiraOperation,
    ToolKind,
    ToolPolicy,
    ToolPolicyEffect,
)
from ai_dlc.domain.identity import (
    InitiativeMembership,
    MembershipNotFoundError,
    Principal,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "configs/initiatives/examples/travel-platform.yaml"
USER = Principal("gateway-user", "test")
ALL_PERMISSIONS = frozenset(ToolPermission)


def profile(initiative, *, jira=True, snow=True, git=True, git_write=True, approval=False):
    document = load_initiative_profile(EXAMPLE).model_dump(mode="json")
    document["initiative"]["id"] = initiative
    document["integrations"]["jira"] = {"enabled": jira, "projects": ["ABC"] if jira else []}
    document["integrations"]["servicenow"] = {
        "enabled": snow,
        "scopes": ["incident"] if snow else [],
        "assignment_groups": [],
    }
    document["integrations"]["git"] = {
        "enabled": git,
        "provider": "github" if git else None,
        "repositories": [
            {
                "id": "repo-one",
                "owner": "trusted-owner",
                "name": "trusted-repository",
                "default_branch": "main",
                "access": "read_write" if git_write else "read_only",
            }
        ]
        if git
        else [],
    }
    document["build_profiles"] = []
    for domain in ("jira", "servicenow", "git"):
        document["policies"][f"{domain}_write"] = {
            "enabled": True,
            "human_approval_required": approval,
        }
    return InitiativeProfile.model_validate(document)


A = profile("initiative-alpha", snow=False, git_write=False)
B = profile("initiative-beta", jira=False, git=True, git_write=True)


def rules(profiles=(A, B)):
    catalog = GatewayCatalog.from_handlers()
    return tuple(
        ToolPolicy(item.initiative.id, tool.operation, ToolPolicyEffect.ALLOW)
        for item in profiles
        for tool in catalog.tools
    )


class World:
    def __init__(
        self,
        *,
        profiles=(A, B),
        permissions=ALL_PERMISSIONS,
        policy_rules=None,
        user=USER,
    ):
        self.user = user
        self.catalog = GatewayCatalog.from_handlers()
        self.registry = InitiativeRegistry(
            InMemoryInitiativeRepository(), InMemoryRegistryEventSink()
        )
        for item in profiles:
            self.registry.create(item)
        memberships = InMemoryMembershipRepository(
            tuple(
                InitiativeMembership(user.subject_id, item.initiative.id, (Role.DEVELOPER,))
                for item in profiles
            )
        )
        roles = RolePolicy((RoleGrant(Role.DEVELOPER, tool_permissions=permissions),))
        self.resolver = GatewayContextResolver(self.registry, memberships, roles)
        self.discovery = GatewayDiscovery(
            self.catalog,
            InMemoryToolPolicyRepository(
                policy_rules if policy_rules is not None else rules(profiles)
            ),
        )

    def context(self, item=A):
        return self.resolver.resolve(
            AuthenticatedRuntimeSelection(
                self.user, item.initiative.id, "dev", "trace-1", "workspace-1", "task-1"
            )
        )


class FakeHandler:
    def __init__(self):
        self.calls = []

    def invoke(self, name, arguments, *, context):
        self.calls.append((name, arguments, context))
        return {"outcome": "success", "operation": name}


class Telemetry:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def router(world):
    jira, snow, git, telemetry = FakeHandler(), FakeHandler(), FakeHandler(), Telemetry()
    return GatewayRouter(world.catalog, world.discovery, jira, snow, git, telemetry), (
        jira,
        snow,
        git,
        telemetry,
    )


def invocations(world):
    return TrustedInvocationRegistry(
        InMemoryInvocationRecordStore(),
        world.resolver,
        gateway_id="gateway-id",
        target_ids={"Jira": "target-id", "ServiceNow": "snow-id", "RemoteGit": "git-id"},
    )


def names(world, item=A):
    return tuple(found.tool.name for found in world.discovery.available_tools(world.context(item)))


def test_catalog_matches_handlers_and_excludes_local_and_delete():
    catalog = GatewayCatalog.from_handlers()
    assert len(catalog.tools) == 20
    assert tuple(tool.name for tool in catalog.tools) == tuple(sorted(catalog.by_name))
    assert len(catalog.by_name) == 20
    assert not any("workspace" in name or "delete" in name for name in catalog.by_name)
    assert {tool.domain for tool in catalog.tools} == {
        ToolKind.JIRA,
        ToolKind.SERVICENOW,
        ToolKind.GIT,
    }
    assert catalog.version == GatewayCatalog.from_handlers().version


def test_duplicate_names_fail():
    tool = GatewayCatalog.from_handlers().tools[0]
    with pytest.raises(ValueError, match="duplicate"):
        GatewayCatalog((tool, tool))


def test_permission_and_disabled_integration_filtering():
    world = World(
        permissions=frozenset(
            {ToolPermission.JIRA_READ, ToolPermission.SERVICENOW_READ, ToolPermission.GIT_READ}
        )
    )
    alpha = names(world, A)
    assert "jira_get_issue" in alpha and "jira_search_issues" in alpha
    assert "jira_create_issue" not in alpha
    assert "servicenow_get_record" not in alpha
    assert "git_remote_get_repository" in alpha
    assert "git_remote_create_branch" not in alpha
    assert "jira_get_issue" not in names(world, B)
    assert "servicenow_get_record" in names(world, B)


def test_no_permission_no_tools():
    assert names(World(permissions=frozenset())) == ()


def test_unregistered_or_unjoined_initiative_cannot_be_selected():
    world = World(profiles=(A,))
    with pytest.raises(InitiativeNotFoundError):
        world.resolver.resolve(
            AuthenticatedRuntimeSelection(USER, B.initiative.id, "dev", "trace-2")
        )
    world.registry.create(B)
    with pytest.raises(MembershipNotFoundError):
        world.resolver.resolve(
            AuthenticatedRuntimeSelection(USER, B.initiative.id, "dev", "trace-2")
        )


def test_narrowed_logical_scope_removes_discovery():
    world = World()
    context = world.resolver.resolve(
        AuthenticatedRuntimeSelection(
            USER,
            A.initiative.id,
            "dev",
            "trace-3",
            scope_restriction=ScopeRestriction(jira_projects=frozenset()),
        )
    )
    assert not any(
        item.tool.domain is ToolKind.JIRA for item in world.discovery.available_tools(context)
    )


def test_multi_initiative_discovery_and_git_repository_access():
    world = World()
    assert "jira_get_issue" in names(world, A)
    assert "jira_get_issue" not in names(world, B)
    assert "servicenow_get_record" in names(world, B)
    assert "servicenow_get_record" not in names(world, A)
    assert "git_remote_create_branch" not in names(world, A)
    assert "git_remote_create_branch" in names(world, B)


def test_explicit_deny_and_approval_visibility_without_side_effects():
    item = profile("initiative-alpha", approval=True)
    policies = tuple(
        ToolPolicy(
            item.initiative.id,
            tool.operation,
            ToolPolicyEffect.DENY
            if tool.operation is JiraOperation.SEARCH
            else ToolPolicyEffect.ALLOW,
        )
        for tool in GatewayCatalog.from_handlers().tools
    )
    world = World(profiles=(item,), policy_rules=policies)
    found = world.discovery.available_tools(world.context(item))
    by_name = {entry.tool.name: entry for entry in found}
    assert "jira_search_issues" not in by_name
    assert "jira_get_issue" in by_name
    assert by_name["jira_create_issue"].approval_required
    assert not by_name["jira_get_issue"].approval_required
    assert world.discovery.available_tools(world.context(item)) == found


def test_git_branch_policy_is_visible_without_guessing_target_branch():
    item = profile("initiative-alpha")
    policies = tuple(
        ToolPolicy(
            item.initiative.id,
            tool.operation,
            ToolPolicyEffect.ALLOW,
            branch_pattern="feature/*" if tool.operation is GitOperation.CREATE_BRANCH else None,
        )
        for tool in GatewayCatalog.from_handlers().tools
    )
    world = World(profiles=(item,), policy_rules=policies)
    assert "git_remote_create_branch" in names(world, item)


def test_router_uses_existing_handlers_and_trusted_context_only():
    world = World()
    entry, (jira, snow, git, telemetry) = router(world)
    context = world.context()
    result = entry.invoke("jira_get_issue", {"project_key": "ABC"}, context=context)
    assert result["outcome"] == "success"
    assert jira.calls[0][2].resource_context.authorization.initiative_id == A.initiative.id
    assert not snow.calls and not git.calls
    assert telemetry.events[-1].tool_name == "jira_get_issue"
    assert "jira_get_issue" in [tool["name"] for tool in entry.list_tools(context=context)]
    with pytest.raises(UnknownGatewayToolError):
        entry.invoke("workspace_build", {}, context=context)


def test_runtime_facade_exposes_filtered_tools_and_uses_gateway_transport():
    world = World(permissions=frozenset({ToolPermission.JIRA_READ}))

    class Transport:
        calls = []

        def invoke(self, name, arguments, *, context, message_id):
            self.calls.append((name, arguments, context, message_id))
            return {"outcome": "success"}

    transport = Transport()
    access = GatewayRuntimeAccess(world.discovery, transport, invocations(world))
    context = world.context()
    assert {item["name"] for item in access.available_tools(context=context)} == {
        "jira_get_issue",
        "jira_search_issues",
    }
    assert all(
        INVOCATION_REF_FIELD not in item["inputSchema"]["properties"]
        for item in access.available_tools(context=context)
    )
    assert access.invoke("jira_get_issue", {}, context=context)["outcome"] == "success"
    assert transport.calls[0][2] is context
    assert INVOCATION_REF_FIELD in transport.calls[0][1]
    with pytest.raises(PermissionError):
        access.invoke("jira_get_issue", {INVOCATION_REF_FIELD: "forged"}, context=context)
    with pytest.raises(UnknownGatewayToolError):
        access.invoke("jira_create_issue", {}, context=context)
    assert len(transport.calls) == 1


def test_hidden_manual_invocation_still_reaches_governed_denial():
    from test_jira_integration import World as JiraWorld

    jira_world = JiraWorld(permissions=frozenset({ToolPermission.JIRA_READ}))
    world = World(permissions=frozenset({ToolPermission.JIRA_READ}))
    entry = GatewayRouter(
        world.catalog, world.discovery, jira_world.mcp, FakeHandler(), FakeHandler(), Telemetry()
    )
    assert "jira_create_issue" not in [
        item["name"] for item in entry.list_tools(context=world.context())
    ]
    result = entry.invoke(
        "jira_create_issue",
        {"project_key": "ABC", "issue_type": "Task", "summary": "Denied"},
        context=world.context(),
    )
    assert result["outcome"] == "error"
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert jira_world.provider.calls == []


def test_agent_arguments_cannot_set_trusted_fields():
    from test_jira_integration import World as JiraWorld

    jira_world = JiraWorld()
    world = World()
    entry = GatewayRouter(
        world.catalog, world.discovery, jira_world.mcp, FakeHandler(), FakeHandler(), Telemetry()
    )
    result = entry.invoke(
        "jira_get_issue",
        {
            "project_key": "ABC",
            "issue_key": "ABC-1",
            "is_admin": True,
            "initiative_id": "initiative-beta",
            "environment": "prod",
        },
        context=world.context(),
    )
    assert result["outcome"] == "error"
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_lambda_target_requires_aws_metadata_and_trusted_lookup():
    world = World()
    entry, _ = router(world)
    registry = invocations(world)
    target = GatewayLambdaTarget(entry, registry, target_name="Jira")
    with pytest.raises(PermissionError):
        target.invoke({}, SimpleNamespace())
    metadata = {
        "bedrockAgentCoreMessageVersion": "1.0",
        "bedrockAgentCoreGatewayId": "gateway-id",
        "bedrockAgentCoreTargetId": "target-id",
        "bedrockAgentCoreAwsRequestId": "request-id",
        "bedrockAgentCoreMcpMessageId": "message-id",
        "bedrockAgentCoreToolName": "Jira___jira_get_issue",
    }
    issued = registry.issue(
        "jira_get_issue",
        {"project_key": "ABC"},
        context=world.context(),
        message_id="message-id",
    )
    response = target.invoke(
        {"project_key": "ABC", INVOCATION_REF_FIELD: issued.reference},
        SimpleNamespace(client_context=SimpleNamespace(custom=metadata)),
    )
    assert response["outcome"] == "success"
    with pytest.raises(PermissionError):
        target.invoke(
            {"project_key": "ABC", INVOCATION_REF_FIELD: issued.reference},
            SimpleNamespace(client_context=SimpleNamespace(custom=metadata)),
        )
    metadata["bedrockAgentCoreMessageVersion"] = "2.0"
    with pytest.raises(PermissionError, match="version"):
        target.invoke({}, SimpleNamespace(client_context=SimpleNamespace(custom=metadata)))
    metadata["bedrockAgentCoreMessageVersion"] = "1.0"
    metadata["bedrockAgentCoreToolName"] = "RemoteGit___git_remote_get_repository"
    with pytest.raises(UnknownGatewayToolError):
        target.invoke({}, SimpleNamespace(client_context=SimpleNamespace(custom=metadata)))
    metadata["bedrockAgentCoreToolName"] = "Jira___servicenow_get_record"
    with pytest.raises(UnknownGatewayToolError):
        target.invoke({}, SimpleNamespace(client_context=SimpleNamespace(custom=metadata)))


def test_invocation_reference_is_bound_to_tool_arguments_metadata_and_one_use():
    world = World()
    registry = invocations(world)
    context = world.context()
    arguments = {"project_key": "ABC"}

    def issue():
        return registry.issue(
            "jira_get_issue", arguments, context=context, message_id="message-id"
        ).reference

    with pytest.raises(PermissionError):
        registry.resolve(
            "missing",
            gateway_id="gateway-id",
            target_id="target-id",
            tool_name="jira_get_issue",
            message_id="message-id",
            arguments=arguments,
        )
    ref = issue()
    with pytest.raises(PermissionError, match="mismatch"):
        registry.resolve(
            ref,
            gateway_id="gateway-id",
            target_id="target-id",
            tool_name="jira_get_issue",
            message_id="wrong-user-session",
            arguments=arguments,
        )
    with pytest.raises(PermissionError, match="unavailable"):
        registry.resolve(
            ref,
            gateway_id="gateway-id",
            target_id="target-id",
            tool_name="jira_get_issue",
            message_id="message-id",
            arguments=arguments,
        )
    ref = issue()
    with pytest.raises(PermissionError, match="mismatch"):
        registry.resolve(
            ref,
            gateway_id="gateway-id",
            target_id="snow-id",
            tool_name="jira_get_issue",
            message_id="message-id",
            arguments=arguments,
        )
    ref = issue()
    with pytest.raises(PermissionError, match="mismatch"):
        registry.resolve(
            ref,
            gateway_id="gateway-id",
            target_id="target-id",
            tool_name="jira_get_issue",
            message_id="message-id",
            arguments={"project_key": "XYZ", "initiative_id": B.initiative.id},
        )
    ref = issue()
    resolved = registry.resolve(
        ref,
        gateway_id="gateway-id",
        target_id="target-id",
        tool_name="jira_get_issue",
        message_id="message-id",
        arguments=arguments,
    )
    assert resolved.resolution.authorization.principal.subject_id == USER.subject_id
    assert resolved.resolution.authorization.initiative_id == A.initiative.id
    with pytest.raises(PermissionError, match="unavailable"):
        registry.resolve(
            ref,
            gateway_id="gateway-id",
            target_id="target-id",
            tool_name="jira_get_issue",
            message_id="message-id",
            arguments=arguments,
        )


def test_expired_or_changed_authorization_correlation_fails_closed(monkeypatch):
    import ai_dlc.application.gateway.invocation as module

    world = World()
    registry = invocations(world)
    context = world.context()
    ref = registry.issue("jira_get_issue", {}, context=context, message_id="m").reference
    monkeypatch.setattr(module, "time", lambda: 10**12)
    with pytest.raises(PermissionError, match="mismatch"):
        registry.resolve(
            ref,
            gateway_id="gateway-id",
            target_id="target-id",
            tool_name="jira_get_issue",
            message_id="m",
            arguments={},
        )
    monkeypatch.undo()
    other = replace(context, initiative_revision=context.initiative_revision + 1)
    ref = registry.issue("jira_get_issue", {}, context=other, message_id="m").reference
    with pytest.raises(PermissionError, match="authorization changed"):
        registry.resolve(
            ref,
            gateway_id="gateway-id",
            target_id="target-id",
            tool_name="jira_get_issue",
            message_id="m",
            arguments={},
        )


def test_correlated_context_cannot_move_to_another_user_or_initiative():
    store = InMemoryInvocationRecordStore()
    alice = World(profiles=(A,), user=Principal("alice", "test"))
    bob = World(profiles=(A,), user=Principal("bob", "test"))
    other_initiative = World(profiles=(B,), user=Principal("alice", "test"))

    def registry(world):
        return TrustedInvocationRegistry(
            store, world.resolver, gateway_id="gateway-id", target_ids={"Jira": "target-id"}
        )

    issuer = registry(alice)
    for receiving_world in (bob, other_initiative):
        ref = issuer.issue(
            "jira_get_issue", {}, context=alice.context(A), message_id="message-id"
        ).reference
        with pytest.raises(PermissionError):
            registry(receiving_world).resolve(
                ref,
                gateway_id="gateway-id",
                target_id="target-id",
                tool_name="jira_get_issue",
                message_id="message-id",
                arguments={},
            )


def test_dynamodb_store_uses_conditional_put_and_atomic_delete_return():
    class Client:
        def __init__(self):
            self.items = {}
            self.writes = []
            self.deletes = []

        def put_item(self, **kwargs):
            self.writes.append(kwargs)
            key = kwargs["Item"]["reference_hash"]["S"]
            if key in self.items:
                raise ValueError("conditional write failed")
            self.items[key] = kwargs["Item"]

        def delete_item(self, **kwargs):
            self.deletes.append(kwargs)
            key = kwargs["Key"]["reference_hash"]["S"]
            item = self.items.pop(key, None)
            return {"Attributes": item} if item else {}

    client = Client()
    store = DynamoDbInvocationRecordStore(client, "invocations")
    store.put("hashed-reference", {"initiative_id": "initiative-alpha"}, 123)
    assert client.writes[0]["ConditionExpression"] == "attribute_not_exists(reference_hash)"
    assert store.take("hashed-reference") == {"initiative_id": "initiative-alpha"}
    assert client.deletes[0]["ReturnValues"] == "ALL_OLD"
    assert store.take("hashed-reference") is None


def test_cloudformation_synthesis_matches_checked_in_template_and_is_scoped():
    catalog = GatewayCatalog.from_handlers()
    expected = synthesize_gateway_template(catalog)
    actual = json.loads((ROOT / "infrastructure/agentcore/gateway.json").read_text())
    assert actual == expected
    resources = actual["Resources"]
    assert resources["EnterpriseGateway"]["Type"] == "AWS::BedrockAgentCore::Gateway"
    assert resources["EnterpriseGateway"]["Properties"]["AuthorizerType"] == "AWS_IAM"
    assert resources["GatewayRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0][
        "Action"
    ] == ["lambda:InvokeFunction"]
    assert resources["RuntimeGatewayInvokePolicy"]["Properties"]["PolicyDocument"]["Statement"][0][
        "Action"
    ] == ["bedrock-agentcore:InvokeGateway"]
    assert resources["InvocationTable"]["Properties"]["TimeToLiveSpecification"]["Enabled"]
    assert resources["RuntimeInvocationWritePolicy"]["Properties"]["PolicyDocument"]["Statement"][
        0
    ]["Action"] == ["dynamodb:PutItem"]
    for label in ("Jira", "ServiceNow", "RemoteGit"):
        assert resources[f"{label}InvocationReadPolicy"]["Properties"]["PolicyDocument"][
            "Statement"
        ][0]["Action"] == ["dynamodb:DeleteItem"]
    assert not any(key.endswith("FunctionArn") for key in actual["Parameters"])
    assert all(
        "${AWS::AccountId}" in json.dumps(resources[key])
        for key in ("JiraTarget", "ServiceNowTarget", "RemoteGitTarget")
    )
    names = []
    for key in ("JiraTarget", "ServiceNowTarget", "RemoteGitTarget"):
        target = resources[key]["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]
        names.extend(item["Name"] for item in target["ToolSchema"]["InlinePayload"])
    assert set(names) == set(catalog.by_name)
    assert len(names) == len(set(names))
    text = json.dumps(actual).lower()
    assert "workspace_" not in text and "secretarn" not in text
    assert "https://" not in text and 'action": "*"' not in text


def test_gateway_schema_is_projected_from_handler_schema():
    catalog = GatewayCatalog.from_handlers()
    template = synthesize_gateway_template(catalog)
    tools = [
        item
        for name in ("JiraTarget", "ServiceNowTarget", "RemoteGitTarget")
        for item in template["Resources"][name]["Properties"]["TargetConfiguration"]["Mcp"][
            "Lambda"
        ]["ToolSchema"]["InlinePayload"]
    ]
    for item in tools:
        source = catalog.by_name[item["Name"]].input_schema
        assert set(item["InputSchema"]["Properties"]) == set(source["properties"]) | {
            INVOCATION_REF_FIELD
        }
        assert item["InputSchema"].get("Required", []) == [
            *source.get("required", []),
            INVOCATION_REF_FIELD,
        ]
        assert not set(source["properties"]) & {
            "site_url",
            "token",
            "connection_alias",
            "secret_arn",
            "bucket",
            "table",
        }


def test_projected_schema_cannot_bypass_strict_request_validation():
    from test_jira_integration import World as JiraWorld

    world = World()
    jira_world = JiraWorld()
    entry = GatewayRouter(
        world.catalog, world.discovery, jira_world.mcp, FakeHandler(), FakeHandler(), Telemetry()
    )
    registry = invocations(world)
    target = GatewayLambdaTarget(entry, registry, target_name="Jira")
    schema = next(
        item["InputSchema"]
        for item in synthesize_gateway_template(world.catalog)["Resources"]["JiraTarget"][
            "Properties"
        ]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
        if item["Name"] == "jira_search_issues"
    )
    assert schema["Properties"]["page_size"] == {"Type": "integer", "Description": "Page Size"}
    arguments = {"project_key": "ABC", "page_size": 101}
    issued = registry.issue(
        "jira_search_issues", arguments, context=world.context(), message_id="search-message"
    )
    metadata = {
        "bedrockAgentCoreMessageVersion": "1.0",
        "bedrockAgentCoreGatewayId": "gateway-id",
        "bedrockAgentCoreTargetId": "target-id",
        "bedrockAgentCoreAwsRequestId": "request-id",
        "bedrockAgentCoreMcpMessageId": "search-message",
        "bedrockAgentCoreToolName": "Jira___jira_search_issues",
    }
    result = target.invoke(
        {**arguments, INVOCATION_REF_FIELD: issued.reference},
        SimpleNamespace(client_context=SimpleNamespace(custom=metadata)),
    )
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert jira_world.provider.calls == []


def test_hidden_servicenow_and_git_writes_still_fail_in_services():
    from test_git_remote_integration import World as GitWorld
    from test_servicenow_integration import World as SnowWorld

    selected = profile("initiative-alpha")
    principal = Principal("shared-user", "test")
    world = World(
        profiles=(selected,),
        user=principal,
        permissions=frozenset({ToolPermission.SERVICENOW_READ, ToolPermission.GIT_READ}),
    )
    snow = SnowWorld(permissions=frozenset({ToolPermission.SERVICENOW_READ}))
    git = GitWorld(permissions=frozenset({ToolPermission.GIT_READ}))
    entry = GatewayRouter(
        world.catalog, world.discovery, FakeHandler(), snow.mcp, git.mcp, Telemetry()
    )
    visible = {item["name"] for item in entry.list_tools(context=world.context(selected))}
    assert "servicenow_create_record" not in visible
    assert "git_remote_create_branch" not in visible
    snow_result = entry.invoke(
        "servicenow_create_record",
        {
            "scope_id": "incident",
            "record_type": "incident",
            "short_description": "Denied",
        },
        context=world.context(selected),
    )
    git_result = entry.invoke(
        "git_remote_create_branch",
        {"repository_id": "repo-one", "branch": "feature/denied", "source_branch": "main"},
        context=world.context(selected),
    )
    assert snow_result["error"]["code"] == "PERMISSION_DENIED"
    assert git_result["error"]["code"] == "PERMISSION_DENIED"
    assert snow.provider.calls == []
    assert not any(adapter.calls for adapter in git.adapters.values())


def test_gateway_telemetry_omits_business_content(caplog):
    import logging

    from ai_dlc.adapters.gateway.telemetry import JsonGatewayTelemetrySink
    from ai_dlc.application.gateway.router import GatewayTelemetryEvent

    logger = logging.getLogger("aidlc-gateway-test")
    sink = JsonGatewayTelemetrySink(logger)
    with caplog.at_level(logging.INFO, logger="aidlc-gateway-test"):
        sink.record(
            GatewayTelemetryEvent(
                "trace", "principal", "initiative-alpha", "invoke", "jira_get_issue", "error", 5
            )
        )
    data = json.loads(caplog.records[-1].message)
    assert set(data) == {
        "correlation_id",
        "principal_id",
        "initiative_id",
        "action",
        "tool_name",
        "outcome",
        "latency_ms",
    }
