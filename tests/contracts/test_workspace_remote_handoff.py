"""Local execution creates trusted artifacts; only remote Git mutates remote state."""

from dataclasses import replace

import pytest
from support.enterprise import BUSINESS_CANARY, NEW_SHA, OTHER, SHA
from support.workspace import WorkspaceHarness

from ai_dlc.application.gateway import UnknownGatewayToolError
from ai_dlc.application.local_workspace import WorkspaceAuditError

pytestmark = pytest.mark.contract


@pytest.fixture
def workspace(world, tmp_path):
    return WorkspaceHarness(world, tmp_path)


def update_arguments():
    return {
        "repository_id": "repo-a",
        "branch": "feature/one",
        "commit_sha": NEW_SHA,
        "expected_head_sha": SHA,
    }


def commit(workspace, *, context=None):
    return workspace.call(
        "commit",
        {
            "repository_id": "repo-a",
            "message": BUSINESS_CANARY,
            "paths": ["app.py"],
        },
        context=context,
    )


def test_local_commit_to_correlated_gateway_remote_branch_update(world, workspace):
    assert workspace.call("prepare")["outcome"] == "success"
    local = commit(workspace)
    assert local["data"]["commit_sha"] == NEW_SHA
    assert world.calls() == [], "local commit must not invoke a remote provider"
    denied = world.runtime.invoke(
        "git_remote_update_branch", update_arguments(), context=world.context()
    )
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert world.calls() == []
    pairs = workspace.approved_commits(world.context())
    assert pairs == frozenset({("repo-a", NEW_SHA)})
    handoff = workspace.handoffs.handoffs[0]
    assert (
        handoff.initiative_id,
        handoff.workspace_id,
        handoff.task_id,
        handoff.repository_id,
        handoff.principal_id,
    ) == ("initiative-alpha", "workspace-one", "task-one", "repo-a", "contract-user")
    approved = world.context(approved_commits=pairs)
    remote = world.runtime.invoke("git_remote_update_branch", update_arguments(), context=approved)
    assert remote["outcome"] == "success"
    assert remote["data"]["head_sha"] == NEW_SHA
    assert world.provider("git").calls[-1].method == "update_branch"
    assert BUSINESS_CANARY not in repr(workspace.audit.events)


@pytest.mark.parametrize("wrong", ["task", "principal", "initiative", "workspace", "repository"])
def test_handoff_cannot_authorize_another_task_principal_or_repository(world, workspace, wrong):
    workspace.call("prepare")
    assert commit(workspace)["outcome"] == "success"
    context = {
        "task": replace(world.context(), task_id="task-other"),
        "principal": world.context(principal=OTHER),
        "initiative": world.context(1),
        "workspace": replace(world.context(), workspace_id="workspace-other"),
        "repository": world.context(),
    }[wrong]
    pairs = workspace.approved_commits(context)
    if wrong == "repository":
        pairs = frozenset({("repo-b", NEW_SHA)})
    else:
        assert pairs == frozenset()
    approved = replace(context, approved_commits=pairs)
    result = world.router.invoke("git_remote_update_branch", update_arguments(), context=approved)
    assert result["error"]["code"] in {"PERMISSION_DENIED", "INVALID_SCOPE"}
    assert world.calls() == []


@pytest.mark.parametrize(
    "field",
    [
        "approved_commits",
        "commit_sha",
        "trusted_handoff",
        "workspace_path",
        "command",
    ],
)
def test_agent_cannot_manufacture_local_commit_handoff(workspace, field):
    workspace.call("prepare")
    result = workspace.call(
        "commit",
        {
            "repository_id": "repo-a",
            "message": "Commit",
            "paths": ["app.py"],
            field: NEW_SHA,
        },
    )
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert workspace.handoffs.handoffs == ()
    assert workspace.executor.calls == []


def test_local_runtime_is_independent_and_business_logs_stay_out_of_audit(world, workspace):
    assert workspace.call("prepare")["outcome"] == "success"
    assert workspace.call("status")["data"]["source"] == "local_workspace"
    assert (
        workspace.call(
            "apply_patch",
            {
                "repository_id": "repo-a",
                "patch": BUSINESS_CANARY,
            },
        )["outcome"]
        == "success"
    )
    for operation in ("build", "test"):
        result = workspace.call(
            operation, {"repository_id": "repo-a", "build_profile_id": "build-one"}
        )
        assert result["data"]["stdout"] == BUSINESS_CANARY
        assert len(result["data"]["stdout"]) <= 8192
    assert ("build", ("python", "build.py")) in workspace.executor.calls
    assert ("test", ("python", "test.py")) in workspace.executor.calls
    assert world.calls() == []
    for tool in (
        "workspace_status",
        "workspace_apply_patch",
        "workspace_commit",
        "workspace_build",
    ):
        assert tool not in world.catalog.by_name
        with pytest.raises(UnknownGatewayToolError):
            world.router.invoke(tool, {}, context=world.context())
    for hidden in (BUSINESS_CANARY, str(workspace.manager.root), "managed-"):
        assert hidden not in repr(workspace.audit.events)


def test_local_audit_failure_does_not_publish_a_commit_handoff(world, workspace):
    workspace.call("prepare")
    workspace.audit.fail = True
    with pytest.raises(WorkspaceAuditError):
        commit(workspace)
    assert workspace.handoffs.handoffs == ()
    assert world.calls() == []


def test_task_keys_drive_local_isolation_and_cleanup_state(world, workspace):
    first = world.context()
    second = replace(first, task_id="task-two")
    assert workspace.call("prepare", context=first)["outcome"] == "success"
    assert workspace.call("prepare", context=second)["outcome"] == "success"
    assert len(workspace.manager.records) == 2
    assert workspace.call("cleanup", context=first)["data"]["state"] == "cleaned"
    assert workspace.call("status", context=first)["error"]["code"] == "WORKSPACE_NOT_ACTIVE"
    assert workspace.call("status", context=second)["outcome"] == "success"
    assert world.calls() == []
