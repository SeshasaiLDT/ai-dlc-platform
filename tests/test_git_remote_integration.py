"""Remote Git governance, normalization, and provider-selection tests."""

from pathlib import Path

import pytest

from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.git_remote import (
    InMemoryGitProviderRegistry,
    InMemoryGitToolAuditSink,
    InMemoryRemoteGitProvider,
)
from ai_dlc.adapters.resource_bindings import (
    InMemoryBindingEventSink,
    InMemoryResourceBindingRepository,
)
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink, InMemoryToolPolicyRepository
from ai_dlc.application.authorization import (
    AuthorizationService,
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
    resolve_authorization_context,
)
from ai_dlc.application.git_remote import (
    BranchInfo,
    DiffFile,
    GitAuditError,
    GitErrorCode,
    GitRemoteMcpToolHandler,
    GovernedRemoteGitService,
    PullRequestInfo,
    RemoteDiff,
    RepositoryInfo,
    TrustedGitContext,
)
from ai_dlc.application.resource_bindings import (
    GitRepositoryBinding,
    ResourceBindingKey,
    ResourceBindingRegistry,
    ResourceType,
    TrustedResolutionContext,
)
from ai_dlc.application.tool_policy import (
    GitOperation,
    ToolPolicy,
    ToolPolicyEffect,
    ToolPolicyService,
)
from ai_dlc.domain.identity import InitiativeMembership, Principal, Role, ToolPermission
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile
from ai_dlc.domain.initiative.enums import GitProvider, RepositoryAccess

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/initiatives/examples/travel-platform.yaml"
USER = Principal("shared-user", "test")
SHA1 = "a" * 40
SHA2 = "b" * 40
OPS = (
    GitOperation.READ_REPOSITORY,
    GitOperation.READ_BRANCH,
    GitOperation.LIST_BRANCHES,
    GitOperation.READ_DIFF,
    GitOperation.READ_PR,
    GitOperation.CREATE_BRANCH,
    GitOperation.PUSH,
    GitOperation.CREATE_PR,
    GitOperation.UPDATE_PR,
)


def profile(initiative, repo, provider=GitProvider.GITHUB, *, writable=True, approval=False):
    document = load_initiative_profile(EXAMPLE).model_dump(mode="json")
    document["initiative"]["id"] = initiative
    document["integrations"]["git"] = {
        "enabled": True,
        "provider": provider.value,
        "repositories": [
            {
                "id": repo,
                "name": "physical-name-in-profile",
                "owner": "placeholder" if provider is GitProvider.GITHUB else None,
                "default_branch": "main",
                "access": RepositoryAccess.READ_WRITE if writable else RepositoryAccess.READ_ONLY,
            }
        ],
    }
    document["build_profiles"] = []
    document["policies"]["git_write"] = {"enabled": True, "human_approval_required": approval}
    return InitiativeProfile.model_validate(document)


A = profile("initiative-alpha", "repo-a")
B = profile("initiative-beta", "repo-b", GitProvider.BITBUCKET)


def provider(repo):
    return InMemoryRemoteGitProvider(
        repositories=(
            RepositoryInfo(repository_id=repo, display_name="Service", default_branch="main"),
        ),
        branches=(
            BranchInfo(repository_id=repo, name="main", head_sha=SHA1),
            BranchInfo(repository_id=repo, name="feature/one", head_sha=SHA2),
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
                        path="src/app.py",
                        change="modified",
                        additions=2,
                        deletions=1,
                        patch="@@ change",
                    ),
                ),
                changed_file_count=1,
                omitted_file_count=0,
                truncated=False,
            ),
        ),
    )


class World:
    def __init__(self, *, profiles=(A, B), permissions=None, policies=None, adapters=None):
        self.profiles = profiles
        self.adapters = (
            adapters
            if adapters is not None
            else {
                GitProvider.GITHUB: provider("repo-a"),
                GitProvider.BITBUCKET: provider("repo-b"),
            }
        )
        self.registry = InMemoryGitProviderRegistry(self.adapters)
        self.audit = InMemoryGitToolAuditSink()
        self.memberships = InMemoryMembershipRepository(
            tuple(
                InitiativeMembership(USER.subject_id, item.initiative.id, (Role.DEVELOPER,))
                for item in profiles
            )
        )
        self.roles = RolePolicy(
            (
                RoleGrant(
                    Role.DEVELOPER,
                    tool_permissions=permissions
                    if permissions is not None
                    else frozenset({ToolPermission.GIT_READ, ToolPermission.GIT_WRITE}),
                ),
            )
        )
        authorization = AuthorizationService(
            self.memberships, self.roles, InMemoryAuthorizationAuditSink()
        )
        self.policy = ToolPolicyService(
            authorization,
            InMemoryToolPolicyRepository(
                policies
                if policies is not None
                else tuple(
                    ToolPolicy(item.initiative.id, op, ToolPolicyEffect.ALLOW)
                    for item in profiles
                    for op in OPS
                )
            ),
            InMemoryToolPolicyAuditSink(),
        )
        self.bindings = ResourceBindingRegistry(
            InMemoryResourceBindingRepository(),
            InMemoryBindingEventSink(),
            environments=frozenset({"dev"}),
        )
        for item in profiles:
            self.bindings.register(
                ResourceBindingKey(
                    "dev",
                    item.initiative.id,
                    ResourceType.GIT_REPOSITORY,
                    item.integrations.git.repositories[0].id,
                ),
                GitRepositoryBinding(
                    item.integrations.git.provider,
                    f"connection-{item.initiative.id}",
                    f"owner-{item.initiative.id}",
                    f"physical-{item.initiative.id}",
                ),
                correlation_id="setup",
            )
        self.service = GovernedRemoteGitService(
            self.policy, self.bindings, self.registry, self.audit
        )
        self.mcp = GitRemoteMcpToolHandler(self.service)

    def context(self, item=A, *, restriction=None, approved_commits=frozenset(), cancelled=False):
        authorization = resolve_authorization_context(
            USER,
            item.initiative.id,
            item,
            self.memberships,
            self.roles,
            scope_restriction=restriction,
        )
        return TrustedGitContext(
            TrustedResolutionContext("dev", item, authorization, "trace-1"),
            initiative_revision=1,
            workspace_id="workspace-1",
            task_id="task-1",
            approved_commits=approved_commits,
            cancelled=cancelled,
        )


def call(world, name, arguments, *, context=None):
    return world.mcp.invoke(name, arguments, context=context or world.context())


def args(repo="repo-a", **other):
    return {"repository_id": repo, **other}


def test_repository_and_branch_reads_route_by_binding_and_initiative():
    world = World()
    result = call(world, "git_remote_get_repository", args())
    assert result["outcome"] == "success" and result["data"]["default_branch"] == "main"
    github = world.adapters[GitProvider.GITHUB]
    assert github.calls[0].connection_alias == "connection-initiative-alpha"
    assert github.calls[0].repository == "physical-initiative-alpha"
    assert "connection-initiative-alpha" not in str(result)
    assert "physical-initiative-alpha" not in str(result)
    assert (
        call(world, "git_remote_get_branch", args(branch="feature/one"))["data"]["head_sha"] == SHA2
    )
    first = call(world, "git_remote_list_branches", args(page_size=1))
    second = call(
        world, "git_remote_list_branches", args(page_size=1, cursor=first["data"]["next_cursor"])
    )
    assert first["data"]["has_more"] and not second["data"]["has_more"]
    assert [item["name"] for item in first["data"]["branches"] + second["data"]["branches"]] == [
        "feature/one",
        "main",
    ]
    beta = call(world, "git_remote_get_repository", args("repo-b"), context=world.context(B))
    assert beta["outcome"] == "success"
    assert world.adapters[GitProvider.BITBUCKET].calls[0].provider is GitProvider.BITBUCKET
    denied = call(world, "git_remote_get_repository", args("repo-a"), context=world.context(B))
    assert denied["error"]["code"] == "INVALID_SCOPE"
    assert len(github.calls) == 4


def test_scope_narrowing_and_physical_routing_injection_denied_before_provider():
    world = World()
    github = world.adapters[GitProvider.GITHUB]
    denied = call(world, "git_remote_get_repository", args("repo-b"))
    narrowed = call(
        world,
        "git_remote_get_repository",
        args(),
        context=world.context(restriction=ScopeRestriction(repository_ids=frozenset())),
    )
    assert denied["error"]["code"] == narrowed["error"]["code"] == "INVALID_SCOPE"
    for field in (
        "connection_alias",
        "provider",
        "owner",
        "remote_url",
        "token",
        "environment",
        "initiative_id",
    ):
        assert (
            call(world, "git_remote_get_repository", args(**{field: "x"}))["error"]["code"]
            == "INVALID_ARGUMENT"
        )
    assert github.calls == []


def test_diff_is_normalized_bounded_and_explicitly_truncated():
    world = World()
    result = call(world, "git_remote_get_diff", args(base_ref="main", head_ref="feature/one"))
    assert result["data"]["files"][0]["path"] == "src/app.py"
    assert result["data"]["truncated"] is False
    with pytest.raises(ValueError):
        RemoteDiff(
            repository_id="repo-a",
            base_ref="main",
            head_ref="feature/one",
            files=(
                DiffFile(path="x", change="modified", additions=1, deletions=0, patch="x" * 4097),
            ),
            changed_file_count=1,
            omitted_file_count=0,
            truncated=False,
        )
    limited = RemoteDiff(
        repository_id="repo-a",
        base_ref="main",
        head_ref="feature/one",
        files=(),
        changed_file_count=1,
        omitted_file_count=1,
        truncated=True,
    )
    assert limited.truncated and limited.omitted_file_count == 1
    with pytest.raises(ValueError):
        DiffFile(path="../secret", change="modified", additions=1, deletions=0)
    with pytest.raises(ValueError):
        DiffFile(path="x", change="modified", additions=1, deletions=0, patch="é" * 3000)
    with pytest.raises(ValueError):
        RemoteDiff(
            repository_id="repo-a",
            base_ref="main",
            head_ref="feature/one",
            files=tuple(
                DiffFile(
                    path=f"file-{index}",
                    change="modified",
                    additions=1,
                    deletions=0,
                    patch="x" * 4096,
                )
                for index in range(9)
            ),
            changed_file_count=9,
            omitted_file_count=0,
            truncated=False,
        )


def test_mismatched_diff_branch_page_and_pr_responses_fail_closed():
    class BadProvider(InMemoryRemoteGitProvider):
        def list_branches(self, context, request):
            self._call("list_branches", context, request.repository_id)
            return {
                "kind": "branch_page",
                "repository_id": request.repository_id,
                "branches": [
                    {
                        "repository_id": "repo-b",
                        "name": "main",
                        "head_sha": SHA1,
                    }
                ],
                "has_more": False,
            }

        def get_diff(self, context, request):
            self._call("get_diff", context, request.repository_id)
            return RemoteDiff(
                repository_id=request.repository_id,
                base_ref="other",
                head_ref=request.head_ref,
                files=(),
                changed_file_count=0,
                omitted_file_count=0,
                truncated=False,
            )

        def get_pull_request(self, context, request):
            self._call("get_pull_request", context, request.repository_id)
            return {
                "kind": "pull_request",
                "repository_id": request.repository_id,
                "number": request.number,
                "title": "Bad",
                "state": "unknown",
                "base_branch": "main",
                "head_branch": "feature/one",
            }

    world = World(adapters={GitProvider.GITHUB: BadProvider()})
    assert (
        call(world, "git_remote_list_branches", args())["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
    assert (
        call(world, "git_remote_get_diff", args(base_ref="main", head_ref="feature/one"))["error"][
            "code"
        ]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
    assert (
        call(world, "git_remote_get_pull_request", args(number=1))["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )


def test_pr_reads_writes_and_write_scope():
    world = World()
    read = call(world, "git_remote_get_pull_request", args(number=1))
    assert read["data"]["title"] == "Change"
    assert (
        call(world, "git_remote_get_pull_request", args(number=999))["error"]["code"]
        == "RESOURCE_NOT_FOUND"
    )
    created_branch = call(
        world, "git_remote_create_branch", args(branch="feature/two", source_branch="main")
    )
    assert created_branch["data"]["name"] == "feature/two"
    created_pr = call(
        world,
        "git_remote_create_pull_request",
        args(
            base_branch="main",
            head_branch="feature/two",
            title="Second",
            description="Sensitive body",
        ),
    )
    assert created_pr["data"]["number"] == 2
    assert world.adapters[GitProvider.GITHUB].description_for("repo-a", 2) == "Sensitive body"
    updated = call(
        world,
        "git_remote_update_pull_request",
        args(number=2, head_branch="feature/two", title="Revised"),
    )
    assert updated["data"]["title"] == "Revised"
    description_update = call(
        world,
        "git_remote_update_pull_request",
        args(number=2, head_branch="feature/two", description="Changed body"),
    )
    assert description_update["outcome"] == "success"
    assert world.adapters[GitProvider.GITHUB].description_for("repo-a", 2) == "Changed body"
    assert "Sensitive body" not in str(world.audit.events)
    assert "Changed body" not in str(world.audit.events)
    assert (
        call(
            world,
            "git_remote_create_branch",
            args("repo-b", branch="feature/no", source_branch="main"),
        )["error"]["code"]
        == "INVALID_SCOPE"
    )


@pytest.mark.parametrize(
    "name, payload",
    [
        ("git_remote_create_branch", args(branch="feature/new", source_branch="main")),
        (
            "git_remote_update_branch",
            args(branch="feature/one", commit_sha=SHA1, expected_head_sha=SHA2),
        ),
        (
            "git_remote_create_pull_request",
            args(base_branch="main", head_branch="feature/one", title="New"),
        ),
        (
            "git_remote_update_pull_request",
            args(number=1, head_branch="feature/one", title="Edited"),
        ),
    ],
)
def test_read_permission_cannot_write(name, payload):
    world = World(permissions=frozenset({ToolPermission.GIT_READ}))
    assert call(world, name, payload)["error"]["code"] == "PERMISSION_DENIED"
    assert world.adapters[GitProvider.GITHUB].calls == []


def test_trusted_sha_handoff_and_conflict():
    world = World()
    denied = call(
        world,
        "git_remote_update_branch",
        args(branch="feature/one", commit_sha=SHA1, expected_head_sha=SHA2),
    )
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert world.adapters[GitProvider.GITHUB].calls == []
    wrong_repo_handoff = call(
        world,
        "git_remote_update_branch",
        args(branch="feature/one", commit_sha=SHA1, expected_head_sha=SHA2),
        context=world.context(approved_commits=frozenset({("repo-b", SHA1)})),
    )
    assert wrong_repo_handoff["error"]["code"] == "PERMISSION_DENIED"
    assert world.adapters[GitProvider.GITHUB].calls == []
    allowed = call(
        world,
        "git_remote_update_branch",
        args(branch="feature/one", commit_sha=SHA1, expected_head_sha=SHA2),
        context=world.context(approved_commits=frozenset({("repo-a", SHA1)})),
    )
    assert allowed["data"]["head_sha"] == SHA1
    stale = call(
        world,
        "git_remote_update_branch",
        args(branch="feature/one", commit_sha=SHA1, expected_head_sha=SHA2),
        context=world.context(approved_commits=frozenset({("repo-a", SHA1)})),
    )
    assert stale["error"]["code"] == "INVALID_ARGUMENT"
    conflict = call(
        world, "git_remote_create_branch", args(branch="feature/one", source_branch="main")
    )
    assert conflict["error"]["code"] == "INVALID_ARGUMENT"
    invalid = call(world, "git_remote_create_branch", args(branch="../main", source_branch="main"))
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"


def test_provider_selection_missing_adapter_policy_denial_and_approval():
    world = World(adapters={GitProvider.GITHUB: provider("repo-a")})
    missing = call(world, "git_remote_get_repository", args("repo-b"), context=world.context(B))
    assert missing["error"]["code"] == "UPSTREAM_AUTH_CONFIGURATION"
    assert world.adapters[GitProvider.GITHUB].calls == []
    policies = tuple(ToolPolicy(A.initiative.id, op, ToolPolicyEffect.DENY) for op in OPS)
    denied_world = World(profiles=(A,), policies=policies)
    assert (
        call(denied_world, "git_remote_get_repository", args())["error"]["code"]
        == "PERMISSION_DENIED"
    )
    assert denied_world.adapters[GitProvider.GITHUB].calls == []
    approval_profile = profile("initiative-approval", "repo-a", approval=True)
    gated = World(profiles=(approval_profile,))
    result = call(
        gated,
        "git_remote_create_branch",
        args(branch="feature/new", source_branch="main"),
        context=gated.context(approval_profile),
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert gated.adapters[GitProvider.GITHUB].calls == []


def test_profile_access_and_existing_branch_pattern_gate_writes():
    readonly = profile("initiative-readonly", "repo-a", writable=False)
    world = World(profiles=(readonly,))
    result = call(
        world,
        "git_remote_create_branch",
        args(branch="feature/new", source_branch="main"),
        context=world.context(readonly),
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert world.adapters[GitProvider.GITHUB].calls == []

    restricted = World(
        profiles=(A,),
        policies=(
            ToolPolicy(
                A.initiative.id,
                GitOperation.CREATE_BRANCH,
                ToolPolicyEffect.ALLOW,
                target_id="repo-a",
                branch_pattern="feature/*",
            ),
        ),
    )
    blocked = call(
        restricted, "git_remote_create_branch", args(branch="main", source_branch="main")
    )
    assert blocked["error"]["code"] == "INVALID_SCOPE"
    assert restricted.adapters[GitProvider.GITHUB].calls == []
    allowed = call(
        restricted, "git_remote_create_branch", args(branch="feature/new", source_branch="main")
    )
    assert allowed["outcome"] == "success"


def test_provider_result_revalidation_errors_and_audit_failure():
    class WrongProvider(InMemoryRemoteGitProvider):
        def get_repository(self, context, request):
            super().get_repository(context, request)
            return RepositoryInfo(
                repository_id="repo-b", display_name="Wrong", default_branch="main"
            )

    bad = World(
        adapters={
            GitProvider.GITHUB: WrongProvider(
                repositories=(
                    RepositoryInfo(repository_id="repo-a", display_name="A", default_branch="main"),
                )
            )
        }
    )
    assert (
        call(bad, "git_remote_get_repository", args())["error"]["code"]
        == "MALFORMED_UPSTREAM_RESPONSE"
    )
    world = World()
    github = world.adapters[GitProvider.GITHUB]
    github.fail_next = GitErrorCode.UPSTREAM_TIMEOUT
    timeout = call(world, "git_remote_get_repository", args())
    assert timeout["error"]["code"] == "UPSTREAM_TIMEOUT" and timeout["error"]["retryable"]
    github.fail_next = GitErrorCode.UPSTREAM_TIMEOUT
    write = call(
        world, "git_remote_create_branch", args(branch="feature/new", source_branch="main")
    )
    assert write["error"]["code"] == "UPSTREAM_TIMEOUT" and not write["error"]["retryable"]

    class BrokenAudit:
        def record(self, event):
            raise RuntimeError("audit unavailable")

    service = GovernedRemoteGitService(world.policy, world.bindings, world.registry, BrokenAudit())
    with pytest.raises(GitAuditError):
        service.invoke(GitOperation.READ_REPOSITORY, args(), context=world.context())


def test_remote_public_catalog_has_no_local_or_physical_fields():
    definitions = GitRemoteMcpToolHandler.definitions()
    assert len(definitions) == 9
    assert not any("commit" in item["name"] or "checkout" in item["name"] for item in definitions)
    schemas = str(definitions).lower()
    for forbidden in (
        "connection_alias",
        "remote_url",
        "owner",
        "provider",
        "token",
        "password",
        "environment",
        "initiative_id",
    ):
        assert forbidden not in schemas
