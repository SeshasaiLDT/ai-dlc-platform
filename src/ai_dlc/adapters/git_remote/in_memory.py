"""Deterministic provider-neutral remote Git fake and adapter registry."""

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType

from ai_dlc.application.git_remote.models import (
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
from ai_dlc.application.git_remote.ports import (
    GitProviderContext,
    GitProviderFailure,
    GitToolAuditEvent,
    RemoteGitProvider,
)
from ai_dlc.domain.initiative.enums import GitProvider as GitProviderType


@dataclass(frozen=True, slots=True)
class ProviderCall:
    operation: str
    provider: GitProviderType
    connection_alias: str
    owner: str
    repository: str
    logical_repository_id: str


class InMemoryRemoteGitProvider:
    def __init__(
        self,
        repositories: tuple[RepositoryInfo, ...] = (),
        branches: tuple[BranchInfo, ...] = (),
        pull_requests: tuple[PullRequestInfo, ...] = (),
        diffs: tuple[RemoteDiff, ...] = (),
    ) -> None:
        self._repositories = {item.repository_id: item for item in repositories}
        self._branches = {(item.repository_id, item.name): item for item in branches}
        self._prs = {(item.repository_id, item.number): item for item in pull_requests}
        self._pr_descriptions: dict[tuple[str, int], str] = {}
        self._diffs = {(item.repository_id, item.base_ref, item.head_ref): item for item in diffs}
        self.calls: list[ProviderCall] = []
        self.fail_next: GitErrorCode | None = None

    def _call(self, operation: str, context: GitProviderContext, repository_id: str) -> None:
        binding = context.binding
        self.calls.append(
            ProviderCall(
                operation,
                binding.provider,
                binding.connection_alias,
                binding.owner,
                binding.repository,
                repository_id,
            )
        )
        if self.fail_next is not None:
            code, self.fail_next = self.fail_next, None
            raise GitProviderFailure(code)

    @staticmethod
    def _found(mapping: dict, key):
        try:
            return mapping[key]
        except KeyError:
            raise GitProviderFailure(GitErrorCode.RESOURCE_NOT_FOUND) from None

    def get_repository(
        self, context: GitProviderContext, request: GetRepositoryRequest
    ) -> RepositoryInfo:
        self._call("get_repository", context, request.repository_id)
        return self._found(self._repositories, request.repository_id)

    def get_branch(self, context: GitProviderContext, request: GetBranchRequest) -> BranchInfo:
        self._call("get_branch", context, request.repository_id)
        return self._found(self._branches, (request.repository_id, request.branch))

    def list_branches(
        self, context: GitProviderContext, request: ListBranchesRequest
    ) -> BranchPage:
        self._call("list_branches", context, request.repository_id)
        branches = sorted(
            (item for (repo, _), item in self._branches.items() if repo == request.repository_id),
            key=lambda item: item.name,
        )
        try:
            start = int(request.cursor) if request.cursor is not None else 0
        except ValueError:
            raise GitProviderFailure(GitErrorCode.INVALID_ARGUMENT) from None
        if start < 0:
            raise GitProviderFailure(GitErrorCode.INVALID_ARGUMENT)
        page = branches[start : start + request.page_size]
        offset = start + len(page)
        has_more = offset < len(branches)
        return BranchPage(
            repository_id=request.repository_id,
            branches=tuple(page),
            next_cursor=str(offset) if has_more else None,
            has_more=has_more,
        )

    def get_diff(self, context: GitProviderContext, request: GetRemoteDiffRequest) -> RemoteDiff:
        self._call("get_diff", context, request.repository_id)
        return self._found(self._diffs, (request.repository_id, request.base_ref, request.head_ref))

    def get_pull_request(
        self, context: GitProviderContext, request: GetPullRequestRequest
    ) -> PullRequestInfo:
        self._call("get_pull_request", context, request.repository_id)
        return self._found(self._prs, (request.repository_id, request.number))

    def create_branch(
        self, context: GitProviderContext, request: CreateBranchRequest
    ) -> BranchInfo:
        self._call("create_branch", context, request.repository_id)
        source = self._found(self._branches, (request.repository_id, request.source_branch))
        key = (request.repository_id, request.branch)
        if key in self._branches:
            raise GitProviderFailure(GitErrorCode.INVALID_ARGUMENT)
        branch = BranchInfo(
            repository_id=request.repository_id, name=request.branch, head_sha=source.head_sha
        )
        self._branches[key] = branch
        return branch

    def update_branch(
        self, context: GitProviderContext, request: UpdateBranchRequest
    ) -> BranchInfo:
        self._call("update_branch", context, request.repository_id)
        previous = self._found(self._branches, (request.repository_id, request.branch))
        if previous.head_sha != request.expected_head_sha:
            raise GitProviderFailure(GitErrorCode.INVALID_ARGUMENT)
        branch = previous.model_copy(update={"head_sha": request.commit_sha})
        self._branches[(request.repository_id, request.branch)] = branch
        return branch

    def create_pull_request(
        self, context: GitProviderContext, request: CreatePullRequestRequest
    ) -> PullRequestInfo:
        self._call("create_pull_request", context, request.repository_id)
        self._found(self._branches, (request.repository_id, request.base_branch))
        self._found(self._branches, (request.repository_id, request.head_branch))
        number = max((n for repo, n in self._prs if repo == request.repository_id), default=0) + 1
        pr = PullRequestInfo(
            repository_id=request.repository_id,
            number=number,
            title=request.title,
            state="open",
            base_branch=request.base_branch,
            head_branch=request.head_branch,
        )
        self._prs[(request.repository_id, number)] = pr
        self._pr_descriptions[(request.repository_id, number)] = request.description
        return pr

    def update_pull_request(
        self, context: GitProviderContext, request: UpdatePullRequestRequest
    ) -> PullRequestInfo:
        self._call("update_pull_request", context, request.repository_id)
        previous = self._found(self._prs, (request.repository_id, request.number))
        if previous.head_branch != request.head_branch:
            raise GitProviderFailure(GitErrorCode.INVALID_ARGUMENT)
        pr = previous.model_copy(update={"title": request.title or previous.title})
        self._prs[(request.repository_id, request.number)] = pr
        if request.description is not None:
            self._pr_descriptions[(request.repository_id, request.number)] = request.description
        return pr

    def description_for(self, repository_id: str, number: int) -> str | None:
        """Test-only state inspection; never part of an agent-visible result."""
        return self._pr_descriptions.get((repository_id, number))


class InMemoryGitProviderRegistry:
    def __init__(self, providers: Mapping[GitProviderType, RemoteGitProvider]) -> None:
        if any(not isinstance(kind, GitProviderType) for kind in providers):
            raise ValueError("invalid Git provider key")
        self._providers = MappingProxyType(dict(providers))

    def get(self, provider: GitProviderType) -> RemoteGitProvider | None:
        return self._providers.get(provider)


class InMemoryGitToolAuditSink:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[GitToolAuditEvent] = []

    def record(self, event: GitToolAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[GitToolAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
