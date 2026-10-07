"""Trusted workspace lifecycle, executor, handoff, and audit ports."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ai_dlc.application.resource_bindings import GitRepositoryBinding

from .models import (
    CommitResult,
    LocalDiff,
    LocalStatus,
    PatchResult,
    ProcessResult,
    TrustedCommitHandoff,
    WorkspaceKey,
    WorkspaceRecord,
)


class WorkspaceFailure(Exception):
    """Sanitized adapter failure; no host paths or output in public messages."""

    def __init__(self, code: str, decision_id: str | None = None) -> None:
        self.code = code
        self.decision_id = decision_id
        super().__init__(code)


class WorkspaceManager(Protocol):
    def prepare(
        self,
        key: WorkspaceKey,
        owner_id: str,
        binding: GitRepositoryBinding,
        default_branch: str,
        timeout_seconds: float,
    ) -> WorkspaceRecord: ...

    def get(self, key: WorkspaceKey, owner_id: str) -> WorkspaceRecord | None: ...

    def set_branch(self, key: WorkspaceKey, owner_id: str, branch: str) -> WorkspaceRecord: ...

    def cleanup(self, key: WorkspaceKey, owner_id: str) -> WorkspaceRecord: ...


class WorkspaceExecutor(Protocol):
    def checkout(self, record: WorkspaceRecord, branch: str, timeout_seconds: float) -> None: ...

    def status(self, record: WorkspaceRecord, timeout_seconds: float) -> LocalStatus: ...

    def diff(self, record: WorkspaceRecord, timeout_seconds: float) -> LocalDiff: ...

    def apply_patch(
        self, record: WorkspaceRecord, patch: str, timeout_seconds: float
    ) -> PatchResult: ...

    def run_configured(
        self,
        record: WorkspaceRecord,
        operation: str,
        argv: tuple[str, ...],
        working_directory: str,
        timeout_seconds: float,
    ) -> ProcessResult: ...

    def commit(
        self,
        record: WorkspaceRecord,
        message: str,
        paths: tuple[str, ...],
        timeout_seconds: float,
    ) -> CommitResult: ...


class CommitHandoffStore(Protocol):
    def record(self, handoff: TrustedCommitHandoff) -> None: ...

    def approved_commits_for(
        self,
        *,
        initiative_id: str,
        workspace_id: str,
        task_id: str,
        principal_id: str,
    ) -> frozenset[tuple[str, str]]: ...


@dataclass(frozen=True, slots=True)
class WorkspaceAuditEvent:
    audit_ref: str
    occurred_at: datetime
    correlation_id: str
    principal_id: str
    initiative_id: str
    workspace_id: str
    task_id: str
    repository_id: str | None
    operation: str
    branch: str | None
    outcome: str
    error_code: str | None
    latency_ms: int
    policy_decision_id: str | None
    commit_sha: str | None


class WorkspaceAuditSink(Protocol):
    def record(self, event: WorkspaceAuditEvent) -> None: ...
