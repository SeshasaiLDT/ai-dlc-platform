"""Compose the real enterprise services around shared offline trust state."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from ai_dlc.adapters.approval import InMemoryApprovalRepository, InMemoryHumanIdentityVerifier
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.gateway import GatewayLambdaTarget, InMemoryInvocationRecordStore
from ai_dlc.adapters.git_remote import InMemoryGitProviderRegistry, InMemoryRemoteGitProvider
from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.adapters.jira import InMemoryJiraProvider
from ai_dlc.adapters.resource_bindings import (
    InMemoryBindingEventSink,
    InMemoryResourceBindingRepository,
)
from ai_dlc.adapters.servicenow import InMemoryServiceNowProvider
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink, InMemoryToolPolicyRepository
from ai_dlc.application.approval import ApprovalService
from ai_dlc.application.authorization import AuthorizationService, RoleGrant, RolePolicy
from ai_dlc.application.gateway import (
    AuthenticatedRuntimeSelection,
    GatewayCatalog,
    GatewayContextResolver,
    GatewayDiscovery,
    GatewayRouter,
    GatewayRuntimeAccess,
    TrustedInvocationRegistry,
)
from ai_dlc.application.git_remote import (
    BranchInfo,
    DiffFile,
    GitErrorCode,
    GitRemoteMcpToolHandler,
    GovernedRemoteGitService,
    PullRequestInfo,
    RemoteDiff,
    RepositoryInfo,
)
from ai_dlc.application.initiatives import InitiativeRegistry
from ai_dlc.application.jira import (
    GovernedJiraService,
    JiraErrorCode,
    JiraIssueDetail,
    JiraMcpToolHandler,
)
from ai_dlc.application.local_workspace.models import POLICY_OPERATIONS
from ai_dlc.application.resource_bindings import (
    GitRepositoryBinding,
    JiraBinding,
    ResourceBindingKey,
    ResourceBindingRegistry,
    ResourceType,
    ServiceNowBinding,
)
from ai_dlc.application.servicenow import (
    GovernedServiceNowService,
    ServiceNowErrorCode,
    ServiceNowMcpToolHandler,
    ServiceNowRecordDetail,
)
from ai_dlc.application.tool_policy import ToolPolicy, ToolPolicyEffect, ToolPolicyService
from ai_dlc.domain.identity import (
    AdminPermission,
    InitiativeMembership,
    Principal,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile
from ai_dlc.domain.initiative.enums import GitProvider

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 8, tzinfo=UTC)
USER = Principal("contract-user", "test")
OTHER = Principal("other-user", "test")
APPROVER = Principal("contract-approver", "test")
SHA = "a" * 40
NEW_SHA = "b" * 40
BUSINESS_CANARY = "sensitive-business-contract-canary"
PROVIDER_CANARY = "provider-secret-contract-canary"
READ_PERMISSIONS = frozenset(
    {ToolPermission.JIRA_READ, ToolPermission.SERVICENOW_READ, ToolPermission.GIT_READ}
)
PERMISSIONS = READ_PERMISSIONS | frozenset(
    {ToolPermission.JIRA_WRITE, ToolPermission.SERVICENOW_WRITE, ToolPermission.GIT_WRITE}
)
DOMAINS = ("jira", "servicenow", "git")
LABELS = {"jira": "Jira", "servicenow": "ServiceNow", "git": "RemoteGit"}
READ_TOOLS = {
    "jira": "jira_get_issue",
    "servicenow": "servicenow_get_record",
    "git": "git_remote_get_repository",
}
SEARCH_TOOLS = {
    "jira": "jira_search_issues",
    "servicenow": "servicenow_search_records",
    "git": "git_remote_list_branches",
}
WRITE_TOOLS = {
    "jira": "jira_create_issue",
    "servicenow": "servicenow_create_record",
    "git": "git_remote_create_branch",
}
READ_METHODS = {"jira": "get_issue", "servicenow": "get_record", "git": "get_repository"}
WRITE_METHODS = {"jira": "create_issue", "servicenow": "create_record", "git": "create_branch"}
ERROR_TYPES = {"jira": JiraErrorCode, "servicenow": ServiceNowErrorCode, "git": GitErrorCode}
SCOPE_FIELDS = {
    "jira": "jira_projects",
    "servicenow": "servicenow_scopes",
    "git": "repository_ids",
}
RESOURCE_TYPES = {
    "jira": ResourceType.JIRA,
    "servicenow": ResourceType.SERVICENOW,
    "git": ResourceType.GIT_REPOSITORY,
}


def profile(index):
    document = load_initiative_profile(
        ROOT / "configs/initiatives/examples/travel-platform.yaml"
    ).model_dump(mode="json")
    suffix = ("alpha", "beta")[index]
    repo = ("repo-a", "repo-b")[index]
    document["initiative"]["id"] = f"initiative-{suffix}"
    document["integrations"]["jira"] = {"enabled": True, "projects": [suffix.upper()]}
    document["integrations"]["servicenow"] = {
        "enabled": True,
        "scopes": [("incident", "request")[index]],
        "assignment_groups": [],
    }
    document["integrations"]["git"] = {
        "enabled": True,
        "provider": ("github", "bitbucket")[index],
        "repositories": [
            {
                "id": repo,
                "owner": "profile-owner" if index == 0 else None,
                "name": "profile-repository",
                "default_branch": "main",
                "access": "read_write",
            }
        ],
    }
    document["build_profiles"] = [
        {
            "id": "build-one",
            "repository_id": repo,
            "language": "python",
            "working_directory": ".",
            "build_command": "python build.py",
            "test_command": "python test.py",
        }
    ]
    for domain in DOMAINS:
        document["policies"][f"{domain}_write"] = {
            "enabled": True,
            "human_approval_required": False,
        }
    return InitiativeProfile.model_validate(document)


def read_arguments(domain, index=0):
    return {
        "jira": {
            "project_key": ("ALPHA", "BETA")[index],
            "issue_key": ("ALPHA-1", "BETA-1")[index],
        },
        "servicenow": {
            "scope_id": ("incident", "request")[index],
            "record_type": ("incident", "request")[index],
            "record_id": ("record-a", "record-b")[index],
        },
        "git": {"repository_id": ("repo-a", "repo-b")[index]},
    }[domain]


def search_arguments(domain, index=0):
    arguments = read_arguments(domain, index)
    return {key: value for key, value in arguments.items() if key not in {"issue_key", "record_id"}}


def write_arguments(domain, index=0):
    return {
        "jira": {"project_key": ("ALPHA", "BETA")[index], "issue_type": "Task", "summary": "New"},
        "servicenow": {
            "scope_id": ("incident", "request")[index],
            "record_type": ("incident", "request")[index],
            "short_description": "New",
        },
        "git": {
            "repository_id": ("repo-a", "repo-b")[index],
            "branch": "feature/new",
            "source_branch": "main",
        },
    }[domain]


def write_requests(domain):
    if domain == "jira":
        target = read_arguments(domain)
        return (
            (WRITE_TOOLS[domain], write_arguments(domain)),
            ("jira_update_issue", {**target, "summary": "Edited"}),
            ("jira_transition_issue", {**target, "transition_id": "Done"}),
            ("jira_add_comment", {**target, "body": BUSINESS_CANARY}),
        )
    if domain == "servicenow":
        target = read_arguments(domain)
        return (
            (WRITE_TOOLS[domain], write_arguments(domain)),
            ("servicenow_update_record", {**target, "state": "closed"}),
            ("servicenow_add_comment", {**target, "channel": "work_note", "body": BUSINESS_CANARY}),
        )
    target = read_arguments(domain)
    return (
        (WRITE_TOOLS[domain], write_arguments(domain)),
        (
            "git_remote_update_branch",
            {
                **target,
                "branch": "feature/one",
                "commit_sha": NEW_SHA,
                "expected_head_sha": SHA,
            },
        ),
        (
            "git_remote_create_pull_request",
            {
                **target,
                "base_branch": "main",
                "head_branch": "feature/one",
                "title": "New",
                "description": BUSINESS_CANARY,
            },
        ),
        (
            "git_remote_update_pull_request",
            {
                **target,
                "number": 1,
                "head_branch": "feature/one",
                "title": "Edited",
            },
        ),
    )


@dataclass
class ProviderCall:
    method: str
    context: object
    request: object


class ProviderSpy:
    """Record port calls while allowing deliberate upstream fault injection."""

    def __init__(self, backend):
        self.backend = backend
        self.calls = []
        self.overrides = {}

    def __getattr__(self, name):
        method = getattr(self.backend, name)

        def invoke(context, request):
            self.calls.append(ProviderCall(name, context, request))
            override = self.overrides.get(name)
            return override(context, request) if override else method(context, request)

        return invoke


class AuditRecorder:
    def __init__(self):
        self.events = []
        self.fail = False

    def record(self, event):
        if self.fail:
            raise RuntimeError(PROVIDER_CANARY)
        self.events.append(event)


class BindingRepositorySpy(InMemoryResourceBindingRepository):
    def __init__(self):
        super().__init__()
        self.lookups = []

    def find(self, key):
        self.lookups.append(key)
        return super().find(key)


def git_provider(repo):
    return InMemoryRemoteGitProvider(
        repositories=(
            RepositoryInfo(repository_id=repo, display_name="Service", default_branch="main"),
        ),
        branches=(
            BranchInfo(repository_id=repo, name="main", head_sha=SHA),
            BranchInfo(repository_id=repo, name="feature/one", head_sha=SHA),
        ),
        pull_requests=(
            PullRequestInfo(
                repository_id=repo,
                number=1,
                title="Change",
                state="open",
                base_branch="main",
                head_branch="feature/one",
            ),
        ),
        diffs=(
            RemoteDiff(
                repository_id=repo,
                base_ref="main",
                head_ref="feature/one",
                files=(
                    DiffFile(
                        path="app.py",
                        change="modified",
                        additions=1,
                        deletions=0,
                        patch=BUSINESS_CANARY,
                    ),
                ),
                changed_file_count=1,
                omitted_file_count=0,
                truncated=False,
            ),
        ),
    )


class LoopbackGatewayTransport:
    """Simulate the documented AgentCore metadata, with no network or AWS."""

    def __init__(self, world):
        self.world = world
        self.last_event = None
        self.last_metadata = None
        self.mutate = lambda event, metadata: None

    def invoke(self, tool_name, arguments, *, context, message_id):
        label = LABELS[tool_name.split("_", 1)[0]]
        event = dict(arguments)
        metadata = {
            "bedrockAgentCoreMessageVersion": "1.0",
            "bedrockAgentCoreGatewayId": "gateway-contract",
            "bedrockAgentCoreTargetId": f"target-{label}",
            "bedrockAgentCoreAwsRequestId": "aws-request-contract",
            "bedrockAgentCoreMcpMessageId": message_id,
            "bedrockAgentCoreToolName": f"{label}___{tool_name}",
        }
        self.mutate(event, metadata)
        self.last_event, self.last_metadata = event, metadata
        return self.deliver(label, event, metadata)

    def deliver(self, label, event, metadata):
        aws_context = SimpleNamespace(client_context=SimpleNamespace(custom=metadata))
        return self.world.targets[label].invoke(event, aws_context)


class EnterpriseWorld:
    def __init__(self, *, permissions=PERMISSIONS, policy_effects=None, profiles=None, omit=()):
        self.profiles = profiles or (profile(0), profile(1))
        self.catalog = GatewayCatalog.from_handlers()
        self.initiatives = InitiativeRegistry(
            InMemoryInitiativeRepository(), InMemoryRegistryEventSink(), clock=lambda: NOW
        )
        self.memberships = InMemoryMembershipRepository(
            tuple(
                InitiativeMembership(principal.subject_id, item.initiative.id, (Role.DEVELOPER,))
                for principal in (USER, OTHER)
                for item in self.profiles
            )
            + tuple(
                InitiativeMembership(APPROVER.subject_id, item.initiative.id, (Role.REVIEWER,))
                for item in self.profiles
            )
        )
        self.roles = RolePolicy(
            (
                RoleGrant(Role.DEVELOPER, tool_permissions=permissions),
                RoleGrant(
                    Role.REVIEWER,
                    admin_permissions=frozenset(
                        {
                            AdminPermission.INITIATIVE_APPROVAL_MANAGE,
                        }
                    ),
                ),
            )
        )
        for item in self.profiles:
            self.initiatives.create(item)
        self.resolver = GatewayContextResolver(self.initiatives, self.memberships, self.roles)
        authorization = AuthorizationService(
            self.memberships, self.roles, InMemoryAuthorizationAuditSink()
        )
        effects = policy_effects or {}
        operations = tuple(tool.operation for tool in self.catalog.tools) + tuple(
            POLICY_OPERATIONS.values()
        )
        self.policy_repository = InMemoryToolPolicyRepository(
            tuple(
                ToolPolicy(item.initiative.id, op, effects.get(op, ToolPolicyEffect.ALLOW))
                for item in self.profiles
                for op in operations
            )
        )
        self.policy = ToolPolicyService(
            authorization, self.policy_repository, InMemoryToolPolicyAuditSink()
        )
        self.approvals = ApprovalService(
            self.policy,
            authorization,
            InMemoryApprovalRepository(),
            InMemoryHumanIdentityVerifier(frozenset({APPROVER.subject_id})),
            clock=lambda: NOW,
        )
        self.binding_repository = BindingRepositorySpy()
        self.binding_events = InMemoryBindingEventSink()
        self.bindings = ResourceBindingRegistry(
            self.binding_repository,
            self.binding_events,
            environments=frozenset({"dev", "stage"}),
            clock=lambda: NOW,
        )
        for index in range(len(self.profiles)):
            for domain in DOMAINS:
                if (domain, index) not in omit:
                    self.bindings.register(
                        self.key(domain, index),
                        self.details(domain, index),
                        correlation_id="binding-setup",
                    )
        self.binding_repository.lookups.clear()
        self.jira = ProviderSpy(
            InMemoryJiraProvider(
                tuple(
                    JiraIssueDetail(
                        issue_key=f"{project}-1",
                        project_key=project,
                        issue_type="Task",
                        summary="Example",
                        status="Open",
                        updated_at=NOW,
                        description=BUSINESS_CANARY,
                    )
                    for project in ("ALPHA", "BETA")
                )
            )
        )
        self.servicenow = ProviderSpy(
            InMemoryServiceNowProvider(
                tuple(
                    ServiceNowRecordDetail(
                        record_id=f"record-{suffix}",
                        number=f"{scope.upper()}-1",
                        scope_id=scope,
                        record_type=scope,
                        short_description="Example",
                        state="open",
                        updated_at=NOW,
                        description=BUSINESS_CANARY,
                    )
                    for suffix, scope in (("a", "incident"), ("b", "request"))
                )
            )
        )
        self.git = {
            GitProvider.GITHUB: ProviderSpy(git_provider("repo-a")),
            GitProvider.BITBUCKET: ProviderSpy(git_provider("repo-b")),
        }
        self.audits = {domain: AuditRecorder() for domain in DOMAINS}
        self.services = {
            "jira": GovernedJiraService(
                self.policy, self.bindings, self.jira, self.audits["jira"], approvals=self.approvals
            ),
            "servicenow": GovernedServiceNowService(
                self.policy,
                self.bindings,
                self.servicenow,
                self.audits["servicenow"],
                approvals=self.approvals,
            ),
            "git": GovernedRemoteGitService(
                self.policy,
                self.bindings,
                InMemoryGitProviderRegistry(self.git),
                self.audits["git"],
                approvals=self.approvals,
            ),
        }
        self.handlers = {
            "jira": JiraMcpToolHandler(self.services["jira"]),
            "servicenow": ServiceNowMcpToolHandler(self.services["servicenow"]),
            "git": GitRemoteMcpToolHandler(self.services["git"]),
        }
        self.discovery = GatewayDiscovery(self.catalog, self.policy_repository)
        self.telemetry = AuditRecorder()
        self.router = GatewayRouter(
            self.catalog,
            self.discovery,
            self.handlers["jira"],
            self.handlers["servicenow"],
            self.handlers["git"],
            self.telemetry,
        )
        self.invocation_store = InMemoryInvocationRecordStore()
        self.invocations = self.invocation_registry()
        self.targets = {
            label: GatewayLambdaTarget(self.router, self.invocations, target_name=label)
            for label in LABELS.values()
        }
        self.transport = LoopbackGatewayTransport(self)
        self.runtime = GatewayRuntimeAccess(self.discovery, self.transport, self.invocations)

    def context(self, index=0, *, principal=USER, **kwargs):
        return self.resolver.resolve(
            AuthenticatedRuntimeSelection(
                principal,
                self.profiles[index].initiative.id,
                "dev",
                "contract-trace",
                workspace_id="workspace-one",
                task_id="task-one",
                **kwargs,
            )
        )

    def invocation_registry(self, resolver=None, store=None):
        return TrustedInvocationRegistry(
            store if store is not None else self.invocation_store,
            resolver or self.resolver,
            gateway_id="gateway-contract",
            target_ids={label: f"target-{label}" for label in LABELS.values()},
        )

    def provider(self, domain, index=0):
        return (
            self.git[(GitProvider.GITHUB, GitProvider.BITBUCKET)[index]]
            if domain == "git"
            else getattr(self, domain)
        )

    def calls(self):
        return (
            self.jira.calls
            + self.servicenow.calls
            + [call for provider in self.git.values() for call in provider.calls]
        )

    def call(self, domain, tool=None, arguments=None, *, context=None):
        context = context or self.context()
        trusted = {
            "jira": context.for_jira,
            "servicenow": context.for_servicenow,
            "git": context.for_git,
        }[domain]()
        return self.handlers[domain].invoke(
            tool or READ_TOOLS[domain],
            arguments if arguments is not None else read_arguments(domain),
            context=trusted,
        )

    def key(self, domain, index=0):
        return ResourceBindingKey(
            "dev",
            self.profiles[index].initiative.id,
            RESOURCE_TYPES[domain],
            ("repo-a", "repo-b")[index] if domain == "git" else domain,
        )

    @staticmethod
    def details(domain, index=0):
        suffix = ("alpha", "beta")[index]
        connection = f"managed-{domain}-{suffix}"
        return {
            "jira": JiraBinding(connection, f"managed-site-{suffix}"),
            "servicenow": ServiceNowBinding(connection, f"managed-instance-{suffix}"),
            "git": GitRepositoryBinding(
                (GitProvider.GITHUB, GitProvider.BITBUCKET)[index],
                connection,
                f"physical-owner-{suffix}",
                f"physical-repository-{suffix}",
            ),
        }[domain]
