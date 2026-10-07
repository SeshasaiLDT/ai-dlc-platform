"""Task-isolated local workspace operations with a local Git mirror only."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.local_workspace import (
    InMemoryCommitHandoffStore,
    InMemoryWorkspaceAuditSink,
    LocalMirrorMaterializer,
    SafeProcessRunner,
    SubprocessWorkspaceExecutor,
    TemporaryWorkspaceManager,
    contained_path,
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
from ai_dlc.application.local_workspace import (
    GovernedWorkspaceService,
    LocalWorkspaceOperation,
    LocalWorkspaceToolHandler,
    TrustedWorkspaceContext,
    WorkspaceAuditError,
    WorkspaceKey,
    WorkspaceState,
)
from ai_dlc.application.local_workspace.models import POLICY_OPERATIONS
from ai_dlc.application.local_workspace.ports import WorkspaceFailure
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
from ai_dlc.domain.initiative.enums import GitProvider

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/initiatives/examples/travel-platform.yaml"
USER = Principal("shared-user", "test")
OTHER = Principal("other-user", "test")
GIT = Path(shutil.which("git"))


def git(*args, cwd):
    return subprocess.run(
        [str(GIT), *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_profile(
    initiative,
    repo,
    *,
    build_command="python build.py",
    test_command="python test.py",
    approval=False,
):
    document = load_initiative_profile(EXAMPLE).model_dump(mode="json")
    document["initiative"]["id"] = initiative
    document["integrations"]["git"] = {
        "enabled": True,
        "provider": "github",
        "repositories": [
            {
                "id": repo,
                "owner": "configuration-owner",
                "name": "configuration-name",
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
            "build_command": build_command,
            "test_command": test_command,
        }
    ]
    document["policies"]["git_write"] = {
        "enabled": True,
        "human_approval_required": approval,
    }
    return InitiativeProfile.model_validate(document)


A = make_profile("initiative-alpha", "repo-a")
B = make_profile("initiative-beta", "repo-b")


def source_repository(path):
    path.mkdir()
    git("init", "--initial-branch=main", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    git("config", "user.email", "test@example.invalid", cwd=path)
    (path / "README.md").write_text("before\n")
    (path / "build.py").write_text(
        "import os\nprint(os.getenv('HOST_SECRET_CANARY', 'missing'))\nprint('build ok')\n"
    )
    (path / "test.py").write_text("print('test ok')\n")
    (path / "sleep.py").write_text("import time\ntime.sleep(2)\n")
    (path / "flood.py").write_text("print('x' * 12000)\n")
    git("add", "--all", cwd=path)
    git("commit", "-m", "initial", cwd=path)
    return path


class World:
    def __init__(self, tmp_path, *, profiles=(A, B), permissions=None, policies=None):
        mirror_root = tmp_path / "mirrors"
        mirror_root.mkdir()
        source = source_repository(mirror_root / "source")
        self.mirror = source
        self.profiles = profiles
        self.memberships = InMemoryMembershipRepository(
            tuple(
                InitiativeMembership(USER.subject_id, profile.initiative.id, (Role.DEVELOPER,))
                for profile in profiles
            )
            + tuple(
                InitiativeMembership(OTHER.subject_id, profile.initiative.id, (Role.DEVELOPER,))
                for profile in profiles
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
        auth = AuthorizationService(self.memberships, self.roles, InMemoryAuthorizationAuditSink())
        self.policy = ToolPolicyService(
            auth,
            InMemoryToolPolicyRepository(
                policies
                if policies is not None
                else tuple(
                    ToolPolicy(profile.initiative.id, policy_op, ToolPolicyEffect.ALLOW)
                    for profile in profiles
                    for policy_op in POLICY_OPERATIONS.values()
                )
            ),
            InMemoryToolPolicyAuditSink(),
        )
        self.bindings = ResourceBindingRegistry(
            InMemoryResourceBindingRepository(),
            InMemoryBindingEventSink(),
            environments=frozenset({"dev"}),
        )
        sources = {}
        for profile in profiles:
            repository_id = profile.integrations.git.repositories[0].id
            owner = f"owner-{profile.initiative.id}"
            name = f"repository-{profile.initiative.id}"
            self.bindings.register(
                ResourceBindingKey(
                    "dev", profile.initiative.id, ResourceType.GIT_REPOSITORY, repository_id
                ),
                GitRepositoryBinding(
                    GitProvider.GITHUB, f"connection-{profile.initiative.id}", owner, name
                ),
                correlation_id="setup",
            )
            sources[(GitProvider.GITHUB, owner, name)] = source
        runner = SafeProcessRunner({"git": GIT, "python": Path(sys.executable)})
        self.manager = TemporaryWorkspaceManager(
            tmp_path / "workspaces", LocalMirrorMaterializer(mirror_root, sources, runner)
        )
        self.executor = SubprocessWorkspaceExecutor(runner)
        self.handoffs = InMemoryCommitHandoffStore()
        self.audit = InMemoryWorkspaceAuditSink()
        self.service = GovernedWorkspaceService(
            self.policy, self.bindings, self.manager, self.executor, self.handoffs, self.audit
        )
        self.handler = LocalWorkspaceToolHandler(self.service)

    def context(
        self,
        profile=A,
        *,
        task="task-one",
        principal=USER,
        restriction=None,
        cancelled=False,
        timeout=10.0,
    ):
        auth = resolve_authorization_context(
            principal,
            profile.initiative.id,
            profile,
            self.memberships,
            self.roles,
            scope_restriction=restriction,
        )
        return TrustedWorkspaceContext(
            TrustedResolutionContext("dev", profile, auth, "trace-1"),
            workspace_id="workspace-one",
            task_id=task,
            initiative_revision=1,
            timeout_seconds=timeout,
            cancelled=cancelled,
        )

    def key(self, profile=A, task="task-one"):
        return WorkspaceKey(
            profile.initiative.id,
            "workspace-one",
            task,
            profile.integrations.git.repositories[0].id,
        )


@pytest.fixture
def world(tmp_path):
    (tmp_path / "workspaces").mkdir()
    return World(tmp_path)


def call(world, operation, arguments=None, *, context=None):
    return world.handler.invoke(
        f"workspace_{operation}",
        arguments or {"repository_id": "repo-a"},
        context=context or world.context(),
    )


def test_prepare_is_task_isolated_idempotent_and_path_free(world):
    first = call(world, "prepare")
    assert first["outcome"] == "success" and first["data"]["state"] == "active"
    assert call(world, "prepare")["outcome"] == "success"
    one = world.manager.get(world.key(), USER.subject_id)
    two_context = world.context(task="task-two")
    assert call(world, "prepare", context=two_context)["outcome"] == "success"
    two = world.manager.get(world.key(task="task-two"), USER.subject_id)
    assert one.root != two.root
    assert "root" not in str(first) and str(one.root) not in str(first)
    assert (
        call(world, "status", context=world.context(task="task-three"))["error"]["code"]
        == "WORKSPACE_NOT_FOUND"
    )
    with pytest.raises(WorkspaceFailure):
        world.manager.get(world.key(), OTHER.subject_id)


def test_multi_initiative_scope_and_other_task_denial(world):
    denied = call(world, "prepare", {"repository_id": "repo-b"})
    assert denied["error"]["code"] == "INVALID_SCOPE"
    beta = call(world, "prepare", {"repository_id": "repo-b"}, context=world.context(B))
    assert beta["outcome"] == "success"
    assert (
        call(world, "status", {"repository_id": "repo-a"}, context=world.context(B))["error"][
            "code"
        ]
        == "INVALID_SCOPE"
    )
    assert (
        call(
            world,
            "prepare",
            context=world.context(restriction=ScopeRestriction(repository_ids=frozenset())),
        )["error"]["code"]
        == "INVALID_SCOPE"
    )


def test_unprepared_task_cannot_patch_build_or_test_another_task(world):
    assert call(world, "prepare")["outcome"] == "success"
    other_task = world.context(task="task-two")
    patch = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-before\n+after\n"
    )
    for operation, payload in (
        ("status", {"repository_id": "repo-a"}),
        ("apply_patch", {"repository_id": "repo-a", "patch": patch}),
        ("build", {"repository_id": "repo-a", "build_profile_id": "build-one"}),
        ("test", {"repository_id": "repo-a", "build_profile_id": "build-one"}),
    ):
        assert (
            call(world, operation, payload, context=other_task)["error"]["code"]
            == "WORKSPACE_NOT_FOUND"
        )
    assert (
        world.manager.get(world.key(), USER.subject_id).root / "README.md"
    ).read_text() == "before\n"


def test_status_checkout_patch_diff_and_commit_handoff(world):
    call(world, "prepare")
    clean = call(world, "status")
    assert clean["data"]["clean"] and clean["data"]["source"] == "local_workspace"
    record = world.manager.get(world.key(), USER.subject_id)
    git("branch", "feature/work", cwd=record.root)
    checkout = call(world, "checkout", {"repository_id": "repo-a", "branch": "feature/work"})
    assert checkout["outcome"] == "success"
    initial_sha = git("rev-list", "--max-parents=0", "HEAD", cwd=record.root)
    detached = call(world, "checkout", {"repository_id": "repo-a", "branch": initial_sha})
    assert detached["outcome"] == "success"
    assert (
        call(world, "checkout", {"repository_id": "repo-a", "branch": "feature/work"})["outcome"]
        == "success"
    )
    assert (
        call(world, "checkout", {"repository_id": "repo-a", "branch": "--upload-pack=x"})["error"][
            "code"
        ]
        == "INVALID_ARGUMENT"
    )
    patch = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-before\n+after\n"
    )
    applied = call(world, "apply_patch", {"repository_id": "repo-a", "patch": patch})
    assert applied["outcome"] == "success"
    diff = call(world, "diff")
    assert diff["data"]["source"] == "local_workspace"
    assert "after" in diff["data"]["patch"] and not diff["data"]["truncated"]
    status = call(world, "status")
    assert "README.md" in status["data"]["modified_files"]
    committed = call(
        world,
        "commit",
        {
            "repository_id": "repo-a",
            "message": "Update README",
            "paths": ["README.md"],
        },
    )
    assert committed["outcome"] == "success"
    sha = committed["data"]["commit_sha"]
    assert world.handoffs.approved_commits_for(
        initiative_id=A.initiative.id,
        workspace_id="workspace-one",
        task_id="task-one",
        principal_id=USER.subject_id,
    ) == frozenset({("repo-a", sha)})
    assert (
        world.handoffs.approved_commits_for(
            initiative_id=A.initiative.id,
            workspace_id="workspace-one",
            task_id="task-one",
            principal_id=OTHER.subject_id,
        )
        == frozenset()
    )
    assert (
        world.handoffs.approved_commits_for(
            initiative_id=A.initiative.id,
            workspace_id="workspace-one",
            task_id="task-two",
            principal_id=USER.subject_id,
        )
        == frozenset()
    )
    assert (
        git("show", "-s", "--format=%an <%ae>", cwd=record.root)
        == "AI-DLC <ai-dlc@invalid.example>"
    )
    forged = call(
        world,
        "commit",
        {
            "repository_id": "repo-a",
            "message": "Fake",
            "paths": ["README.md"],
            "approved_commits": [["repo-a", "f" * 40]],
        },
    )
    assert forged["error"]["code"] == "INVALID_ARGUMENT"
    magic_path = call(
        world,
        "commit",
        {
            "repository_id": "repo-a",
            "message": "No",
            "paths": [":(top)*"],
        },
    )
    assert magic_path["error"]["code"] == "INVALID_ARGUMENT"


def test_patch_and_path_escape_rejected(world, tmp_path):
    call(world, "prepare")
    record = world.manager.get(world.key(), USER.subject_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (record.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceFailure):
        contained_path(record, "../outside", allow_missing=True)
    with pytest.raises(WorkspaceFailure):
        contained_path(record, "link/escape", allow_missing=True)
    for patch in (
        "diff --git a/../outside b/../outside\n--- a/../outside\n+++ b/../outside\n",
        "diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n",
        (
            "diff --git a/link/escape b/link/escape\n--- a/link/escape\n"
            "+++ b/link/escape\n@@ -0,0 +1 @@\n+bad\n"
        ),
        "x" * 65537,
        "not a diff",
    ):
        assert call(world, "apply_patch", {"repository_id": "repo-a", "patch": patch})["error"][
            "code"
        ] in {"PATCH_REJECTED", "INVALID_ARGUMENT"}
    assert not (outside / "escape").exists()


def test_local_diff_signals_patch_and_file_count_truncation(world):
    call(world, "prepare")
    record = world.manager.get(world.key(), USER.subject_id)
    (record.root / "README.md").write_text("x" * 5000 + "\n")
    for index in range(105):
        (record.root / f"file-{index}").write_text("new\n")
    git("add", "--all", cwd=record.root)
    diff = call(world, "diff")
    assert diff["outcome"] == "success"
    assert diff["data"]["truncated"]
    assert len(diff["data"]["changed_files"]) == 100
    assert diff["data"]["omitted_file_count"] == 6
    assert len(diff["data"]["patch"].encode("utf-8")) <= 32768


def test_build_test_configuration_environment_and_output(world, monkeypatch):
    monkeypatch.setenv("HOST_SECRET_CANARY", "very-secret")
    call(world, "prepare")
    build = call(world, "build", {"repository_id": "repo-a", "build_profile_id": "build-one"})
    test = call(world, "test", {"repository_id": "repo-a", "build_profile_id": "build-one"})
    assert build["data"]["success"] and "missing" in build["data"]["stdout"]
    assert "very-secret" not in str(build)
    assert test["data"]["success"] and "test ok" in test["data"]["stdout"]
    assert (
        call(
            world,
            "build",
            {"repository_id": "repo-a", "build_profile_id": "build-one", "command": "sh"},
        )["error"]["code"]
        == "INVALID_ARGUMENT"
    )
    assert (
        call(world, "build", {"repository_id": "repo-a", "build_profile_id": "missing"})["error"][
            "code"
        ]
        == "INVALID_SCOPE"
    )
    assert "stdout" not in str(world.audit.events)


def test_output_bounds_timeout_and_unsafe_profile_command(tmp_path):
    (tmp_path / "workspaces").mkdir()
    specialized = make_profile(
        "initiative-special",
        "repo-a",
        build_command="python flood.py",
        test_command="python sleep.py",
    )
    world = World(tmp_path, profiles=(specialized,))
    context = world.context(specialized)
    assert call(world, "prepare", context=context)["outcome"] == "success"
    build = call(
        world,
        "build",
        {"repository_id": "repo-a", "build_profile_id": "build-one"},
        context=context,
    )
    assert build["data"]["stdout_truncated"]
    assert len(build["data"]["stdout"].encode("utf-8")) <= 8192
    timeout = call(
        world,
        "test",
        {"repository_id": "repo-a", "build_profile_id": "build-one"},
        context=world.context(specialized, timeout=0.1),
    )
    assert timeout["error"]["code"] == "EXECUTION_TIMEOUT"

    unsafe = make_profile(
        "initiative-unsafe", "repo-a", build_command="python build.py; echo escape"
    )
    (tmp_path / "unsafe" / "workspaces").mkdir(parents=True)
    other = World(tmp_path / "unsafe", profiles=(unsafe,))
    assert call(other, "prepare", context=other.context(unsafe))["outcome"] == "success"
    blocked = call(
        other,
        "build",
        {"repository_id": "repo-a", "build_profile_id": "build-one"},
        context=other.context(unsafe),
    )
    assert blocked["error"]["code"] == "RUNTIME_CONFIGURATION"

    failing = make_profile("initiative-failing", "repo-a", test_command="python absent.py")
    (tmp_path / "failing" / "workspaces").mkdir(parents=True)
    failed_world = World(tmp_path / "failing", profiles=(failing,))
    assert (
        call(failed_world, "prepare", context=failed_world.context(failing))["outcome"] == "success"
    )
    result = call(
        failed_world,
        "test",
        {"repository_id": "repo-a", "build_profile_id": "build-one"},
        context=failed_world.context(failing),
    )
    assert result["outcome"] == "success"
    assert not result["data"]["success"] and result["data"]["exit_code"] != 0


def test_cleanup_is_idempotent_and_owner_bound(world):
    assert call(world, "cleanup")["error"]["code"] == "WORKSPACE_NOT_FOUND"
    call(world, "prepare")
    record = world.manager.get(world.key(), USER.subject_id)
    with pytest.raises(WorkspaceFailure):
        world.manager.cleanup(world.key(), OTHER.subject_id)
    first = call(world, "cleanup")
    second = call(world, "cleanup")
    assert first["data"]["state"] == second["data"]["state"] == "cleaned"
    assert not record.root.exists()
    assert call(world, "status")["error"]["code"] == "WORKSPACE_NOT_ACTIVE"


def test_status_count_is_bounded_and_cleanup_failure_is_retryable(world, monkeypatch):
    call(world, "prepare")
    record = world.manager.get(world.key(), USER.subject_id)
    for index in range(105):
        (record.root / f"untracked-{index}").write_text("x")
    status = call(world, "status")
    assert status["data"]["truncated"]
    assert len(status["data"]["untracked_files"]) == 100
    assert status["data"]["omitted_file_count"] == 5

    from ai_dlc.adapters.local_workspace import manager as manager_module

    original = manager_module.shutil.rmtree

    def fail_once(path):
        raise OSError("simulated cleanup error")

    monkeypatch.setattr(manager_module.shutil, "rmtree", fail_once)
    failed = call(world, "cleanup")
    assert failed["error"]["code"] == "EXECUTION_FAILED"
    assert world.manager.get(world.key(), USER.subject_id).state is WorkspaceState.FAILED
    monkeypatch.setattr(manager_module.shutil, "rmtree", original)
    assert call(world, "cleanup")["data"]["state"] == "cleaned"


def test_replaced_workspace_root_fails_closed_and_cleanup_keeps_external_files(world, tmp_path):
    call(world, "prepare")
    record = world.manager.get(world.key(), USER.subject_id)
    external = tmp_path / "external"
    external.mkdir()
    (external / "retain.txt").write_text("retain")
    original_root = record.root.with_name("original-repo")
    record.root.rename(original_root)
    record.root.symlink_to(external, target_is_directory=True)
    assert call(world, "status")["error"]["code"] == "WORKSPACE_NOT_ACTIVE"
    assert call(world, "cleanup")["data"]["state"] == "cleaned"
    assert (external / "retain.txt").read_text() == "retain"


def test_policy_read_write_and_approval_denials_prevent_execution(tmp_path):
    (tmp_path / "workspaces").mkdir()
    world = World(tmp_path, permissions=frozenset({ToolPermission.GIT_READ}))
    assert call(world, "prepare")["outcome"] == "success"
    assert call(world, "status")["outcome"] == "success"
    before = world.manager.get(world.key(), USER.subject_id).branch
    assert (
        call(world, "checkout", {"repository_id": "repo-a", "branch": "main"})["error"]["code"]
        == "PERMISSION_DENIED"
    )
    assert world.manager.get(world.key(), USER.subject_id).branch == before
    assert (
        call(world, "commit", {"repository_id": "repo-a", "message": "No", "paths": ["README.md"]})[
            "error"
        ]["code"]
        == "PERMISSION_DENIED"
    )
    assert world.handoffs.handoffs == ()

    (tmp_path / "second" / "workspaces").mkdir(parents=True)
    gated_profile = make_profile("initiative-gated", "repo-a", approval=True)
    gated = World(tmp_path / "second", profiles=(gated_profile,))
    assert call(gated, "prepare", context=gated.context(gated_profile))["outcome"] == "success"
    denied = call(
        gated,
        "build",
        {"repository_id": "repo-a", "build_profile_id": "build-one"},
        context=gated.context(gated_profile),
    )
    assert denied["error"]["code"] == "PERMISSION_DENIED"


def test_exact_local_policy_denial_stops_executor(tmp_path):
    (tmp_path / "workspaces").mkdir()
    world = World(
        tmp_path,
        profiles=(A,),
        policies=(
            ToolPolicy(A.initiative.id, GitOperation.PREPARE_WORKSPACE, ToolPolicyEffect.ALLOW),
            ToolPolicy(A.initiative.id, GitOperation.LOCAL_STATUS, ToolPolicyEffect.DENY),
        ),
    )
    assert call(world, "prepare")["outcome"] == "success"

    def forbidden(*args):
        raise AssertionError("executor must not be called")

    world.executor.status = forbidden
    denied = call(world, "status")
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert world.audit.events[-1].policy_decision_id is not None


def test_public_schemas_no_shell_path_credentials_or_handoff():
    definitions = LocalWorkspaceToolHandler.definitions()
    assert len(definitions) == 9
    schema = str(definitions).lower()
    for forbidden in (
        "workspace_path",
        "workspace_root",
        "host_path",
        "command",
        "shell",
        "token",
        "password",
        "credential",
        "approved_commit",
        "connection_alias",
        "remote_url",
    ):
        assert forbidden not in schema


def test_audit_failure_blocks_result(world):
    class BrokenAudit:
        def record(self, event):
            raise RuntimeError("audit unavailable")

    service = GovernedWorkspaceService(
        world.policy, world.bindings, world.manager, world.executor, world.handoffs, BrokenAudit()
    )
    with pytest.raises(WorkspaceAuditError):
        service.invoke(
            LocalWorkspaceOperation.PREPARE, {"repository_id": "repo-a"}, context=world.context()
        )
    record = world.manager.get(world.key(), USER.subject_id)
    (record.root / "README.md").write_text("changed\n")
    with pytest.raises(WorkspaceAuditError):
        service.invoke(
            LocalWorkspaceOperation.COMMIT,
            {"repository_id": "repo-a", "message": "Change", "paths": ["README.md"]},
            context=world.context(),
        )
    assert world.handoffs.handoffs == ()
