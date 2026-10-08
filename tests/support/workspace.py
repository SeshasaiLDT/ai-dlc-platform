"""Finite local fakes for cross-boundary handoff tests, with no subprocesses."""

from dataclasses import replace

from ai_dlc.adapters.local_workspace import InMemoryCommitHandoffStore
from ai_dlc.application.local_workspace import (
    GovernedWorkspaceService,
    LocalWorkspaceToolHandler,
    TrustedWorkspaceContext,
    WorkspaceState,
)
from ai_dlc.application.local_workspace.models import (
    CommitResult,
    LocalStatus,
    PatchResult,
    ProcessResult,
    WorkspaceRecord,
)
from ai_dlc.application.local_workspace.ports import WorkspaceFailure
from support.enterprise import BUSINESS_CANARY, NEW_SHA, NOW, SHA, AuditRecorder


class FakeWorkspaceManager:
    def __init__(self, root):
        self.root = root
        self.records = {}

    def get(self, key, owner_id):
        record = self.records.get(key)
        if record is not None and record.owner_principal_id != owner_id:
            raise WorkspaceFailure("INVALID_SCOPE")
        return record

    def prepare(self, key, owner_id, binding, default_branch, timeout_seconds):
        current = self.get(key, owner_id)
        if current is None:
            root = self.root / key.task_id / key.repository_id
            current = WorkspaceRecord(
                key, owner_id, root, root / "home", WorkspaceState.ACTIVE, default_branch, NOW
            )
            self.records[key] = current
        return current

    def set_branch(self, key, owner_id, branch):
        record = replace(self.get(key, owner_id), branch=branch)
        self.records[key] = record
        return record

    def cleanup(self, key, owner_id):
        record = replace(self.get(key, owner_id), state=WorkspaceState.CLEANED)
        self.records[key] = record
        return record


class FakeWorkspaceExecutor:
    def __init__(self):
        self.calls = []

    def status(self, record, timeout_seconds):
        self.calls.append("status")
        return LocalStatus(
            repository_id=record.key.repository_id,
            branch=record.branch,
            head_sha=SHA,
            staged_files=(),
            modified_files=(),
            untracked_files=(),
            omitted_file_count=0,
            truncated=False,
            clean=True,
        )

    def apply_patch(self, record, patch, timeout_seconds):
        self.calls.append("apply_patch")
        return PatchResult(repository_id=record.key.repository_id, changed_files=("app.py",))

    def run_configured(self, record, operation, argv, working_directory, timeout_seconds):
        self.calls.append((operation, argv))
        return ProcessResult(
            repository_id=record.key.repository_id,
            operation=operation,
            exit_code=0,
            success=True,
            duration_ms=1,
            stdout=BUSINESS_CANARY,
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    def commit(self, record, message, paths, timeout_seconds):
        self.calls.append("commit")
        return CommitResult(repository_id=record.key.repository_id, commit_sha=NEW_SHA)


class WorkspaceHarness:
    def __init__(self, world, root):
        self.world = world
        self.manager = FakeWorkspaceManager(root)
        self.executor = FakeWorkspaceExecutor()
        self.handoffs = InMemoryCommitHandoffStore()
        self.audit = AuditRecorder()
        self.handler = LocalWorkspaceToolHandler(
            GovernedWorkspaceService(
                world.policy,
                world.bindings,
                self.manager,
                self.executor,
                self.handoffs,
                self.audit,
                approvals=world.approvals,
            )
        )

    def call(self, operation, arguments=None, *, context=None):
        context = context or self.world.context()
        trusted = TrustedWorkspaceContext(
            context.resolution,
            context.workspace_id,
            context.task_id,
            initiative_revision=context.initiative_revision,
            approval_id=context.approval_id,
            timeout_seconds=context.timeout_seconds,
            cancelled=context.cancelled,
        )
        return self.handler.invoke(
            f"workspace_{operation}",
            arguments if arguments is not None else {"repository_id": "repo-a"},
            context=trusted,
        )

    def approved_commits(self, context):
        return self.handoffs.approved_commits_for(
            initiative_id=context.resolution.authorization.initiative_id,
            workspace_id=context.workspace_id,
            task_id=context.task_id,
            principal_id=context.resolution.authorization.principal.subject_id,
        )
