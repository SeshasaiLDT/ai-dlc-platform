"""Provider-neutral remote Git ports and trusted audit records."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ai_dlc.application.resource_bindings import GitRepositoryBinding
from ai_dlc.application.tool_policy import GitOperation
from ai_dlc.domain.initiative.enums import GitProvider as GitProviderType

from .models import (
    BranchInfo,
    BranchPage,
    CreateBranchRequest,
    CreatePullRequestRequest,
    GetBranchRequest,
    GetPullRequestRequest,
    GetRemoteDiffRequest,
    GetRepositoryRequest,
    GitErrorCode,
    ListBranchesRequest,
    PullRequestInfo,
    RemoteDiff,
    RepositoryInfo,
    UpdateBranchRequest,
    UpdatePullRequestRequest,
)


@dataclass(frozen=True, slots=True)
class GitProviderContext:
    """Adapter-only binding with managed alias and trusted repository coordinates."""

    binding: GitRepositoryBinding
    correlation_id: str
    timeout_seconds: float


class GitProviderFailure(Exception):
    def __init__(self, code: GitErrorCode) -> None:
        if code not in {
            GitErrorCode.INVALID_ARGUMENT,  # provider conflict or non-fast-forward
            GitErrorCode.RESOURCE_NOT_FOUND,
            GitErrorCode.UPSTREAM_AUTH_CONFIGURATION,
            GitErrorCode.UPSTREAM_TIMEOUT,
            GitErrorCode.MALFORMED_UPSTREAM_RESPONSE,
            GitErrorCode.TRANSIENT_UPSTREAM_FAILURE,
        }:
            raise ValueError("unsupported provider failure code")
        self.code = code
        super().__init__(code.value)


class RemoteGitProvider(Protocol):
    def get_repository(
        self, context: GitProviderContext, request: GetRepositoryRequest
    ) -> RepositoryInfo: ...
    def get_branch(self, context: GitProviderContext, request: GetBranchRequest) -> BranchInfo: ...
    def list_branches(
        self, context: GitProviderContext, request: ListBranchesRequest
    ) -> BranchPage: ...
    def get_diff(
        self, context: GitProviderContext, request: GetRemoteDiffRequest
    ) -> RemoteDiff: ...
    def get_pull_request(
        self, context: GitProviderContext, request: GetPullRequestRequest
    ) -> PullRequestInfo: ...
    def create_branch(
        self, context: GitProviderContext, request: CreateBranchRequest
    ) -> BranchInfo: ...
    def update_branch(
        self, context: GitProviderContext, request: UpdateBranchRequest
    ) -> BranchInfo: ...
    def create_pull_request(
        self, context: GitProviderContext, request: CreatePullRequestRequest
    ) -> PullRequestInfo: ...
    def update_pull_request(
        self, context: GitProviderContext, request: UpdatePullRequestRequest
    ) -> PullRequestInfo: ...


class RemoteGitProviderRegistry(Protocol):
    def get(self, provider: GitProviderType) -> RemoteGitProvider | None: ...


@dataclass(frozen=True, slots=True)
class GitToolAuditEvent:
    audit_ref: str
    occurred_at: datetime
    correlation_id: str
    principal_id: str
    initiative_id: str
    workspace_id: str | None
    task_id: str | None
    operation: GitOperation
    repository_id: str | None
    branch: str | None
    pull_request_number: int | None
    outcome: str
    error_code: GitErrorCode | None
    latency_ms: int
    policy_decision_id: str | None


class GitToolAuditSink(Protocol):
    def record(self, event: GitToolAuditEvent) -> None: ...
