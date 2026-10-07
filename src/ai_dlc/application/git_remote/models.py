"""Provider-neutral logical remote Git requests and bounded results."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ai_dlc.application.resource_bindings import TrustedResolutionContext
from ai_dlc.application.tool_policy import GitOperation

RepoId = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=80)
]
Branch = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", max_length=128)]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Body = Annotated[str, StringConstraints(max_length=10000)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Path = Annotated[str, StringConstraints(min_length=1, max_length=512)]


def valid_branch(value: str) -> bool:
    return ".." not in value and not value.endswith(("/", ".")) and not value.startswith("-")


class RemoteGitModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepoRequest(RemoteGitModel):
    repository_id: RepoId


class GetRepositoryRequest(RepoRequest):
    pass


class GetBranchRequest(RepoRequest):
    branch: Branch

    @model_validator(mode="after")
    def valid_ref(self) -> Self:
        if not valid_branch(self.branch):
            raise ValueError("invalid branch")
        return self


class ListBranchesRequest(RepoRequest):
    page_size: int = Field(default=25, ge=1, le=100)
    cursor: Cursor | None = None


class GetRemoteDiffRequest(RepoRequest):
    base_ref: Branch
    head_ref: Branch

    @model_validator(mode="after")
    def valid_refs(self) -> Self:
        if not valid_branch(self.base_ref) or not valid_branch(self.head_ref):
            raise ValueError("invalid branch")
        return self


class GetPullRequestRequest(RepoRequest):
    number: int = Field(ge=1)


class CreateBranchRequest(RepoRequest):
    branch: Branch
    source_branch: Branch

    @model_validator(mode="after")
    def valid_refs(self) -> Self:
        if not valid_branch(self.branch) or not valid_branch(self.source_branch):
            raise ValueError("invalid branch")
        return self


class UpdateBranchRequest(RepoRequest):
    branch: Branch
    commit_sha: Sha
    expected_head_sha: Sha

    @model_validator(mode="after")
    def valid_ref(self) -> Self:
        if not valid_branch(self.branch):
            raise ValueError("invalid branch")
        return self


class CreatePullRequestRequest(RepoRequest):
    base_branch: Branch
    head_branch: Branch
    title: ShortText
    description: Body = ""

    @model_validator(mode="after")
    def valid_refs(self) -> Self:
        if (
            not valid_branch(self.base_branch)
            or not valid_branch(self.head_branch)
            or self.base_branch == self.head_branch
        ):
            raise ValueError("invalid pull request refs")
        return self


class UpdatePullRequestRequest(RepoRequest):
    number: int = Field(ge=1)
    head_branch: Branch
    title: ShortText | None = None
    description: Body | None = None

    @model_validator(mode="after")
    def valid_patch(self) -> Self:
        if not valid_branch(self.head_branch) or (self.title is None and self.description is None):
            raise ValueError("invalid pull request update")
        return self


REQUEST_TYPES: dict[GitOperation, type[RemoteGitModel]] = {
    GitOperation.READ_REPOSITORY: GetRepositoryRequest,
    GitOperation.READ_BRANCH: GetBranchRequest,
    GitOperation.LIST_BRANCHES: ListBranchesRequest,
    GitOperation.READ_DIFF: GetRemoteDiffRequest,
    GitOperation.READ_PR: GetPullRequestRequest,
    GitOperation.CREATE_BRANCH: CreateBranchRequest,
    GitOperation.PUSH: UpdateBranchRequest,
    GitOperation.CREATE_PR: CreatePullRequestRequest,
    GitOperation.UPDATE_PR: UpdatePullRequestRequest,
}


class RepositoryInfo(RemoteGitModel):
    kind: Literal["repository"] = "repository"
    repository_id: RepoId
    display_name: ShortText
    default_branch: Branch
    archived: bool = False

    @field_validator("default_branch")
    @classmethod
    def valid_default(cls, value: str) -> str:
        if not valid_branch(value):
            raise ValueError("invalid default branch")
        return value


class BranchInfo(RemoteGitModel):
    kind: Literal["branch"] = "branch"
    repository_id: RepoId
    name: Branch
    head_sha: Sha
    protected: bool = False

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not valid_branch(value):
            raise ValueError("invalid branch")
        return value


class BranchPage(RemoteGitModel):
    kind: Literal["branch_page"] = "branch_page"
    repository_id: RepoId
    branches: tuple[BranchInfo, ...]
    next_cursor: Cursor | None = None
    has_more: bool

    @model_validator(mode="after")
    def consistent_cursor(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("cursor and has_more disagree")
        return self


class DiffFile(RemoteGitModel):
    path: Path
    change: Literal["added", "modified", "deleted", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    patch: Annotated[str, StringConstraints(max_length=4096)] | None = None

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("invalid changed-file path")
        return value

    @field_validator("patch")
    @classmethod
    def bounded_patch_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 4096:
            raise ValueError("patch exceeds byte limit")
        return value


class RemoteDiff(RemoteGitModel):
    kind: Literal["remote_diff"] = "remote_diff"
    repository_id: RepoId
    base_ref: Branch
    head_ref: Branch
    files: tuple[DiffFile, ...] = Field(max_length=100)
    changed_file_count: int = Field(ge=0)
    omitted_file_count: int = Field(ge=0)
    patches_truncated: bool = False
    truncated: bool

    @field_validator("base_ref", "head_ref")
    @classmethod
    def valid_ref(cls, value: str) -> str:
        if not valid_branch(value):
            raise ValueError("invalid diff ref")
        return value

    @model_validator(mode="after")
    def bounded_and_consistent(self) -> Self:
        if (
            self.changed_file_count != len(self.files) + self.omitted_file_count
            or self.truncated != (self.omitted_file_count > 0 or self.patches_truncated)
            or sum(len((item.patch or "").encode("utf-8")) for item in self.files) > 32768
        ):
            raise ValueError("invalid diff limits/counts")
        return self


class PullRequestInfo(RemoteGitModel):
    kind: Literal["pull_request"] = "pull_request"
    repository_id: RepoId
    number: int = Field(ge=1)
    title: ShortText
    state: Literal["open", "closed", "merged"]
    base_branch: Branch
    head_branch: Branch
    author_display: ShortText | None = None

    @field_validator("base_branch", "head_branch")
    @classmethod
    def valid_ref(cls, value: str) -> str:
        if not valid_branch(value):
            raise ValueError("invalid pull request ref")
        return value


GitData = Annotated[
    RepositoryInfo | BranchInfo | BranchPage | RemoteDiff | PullRequestInfo,
    Field(discriminator="kind"),
]


class GitErrorCode(StrEnum):
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_SCOPE = "INVALID_SCOPE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    UPSTREAM_AUTH_CONFIGURATION = "UPSTREAM_AUTH_CONFIGURATION"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    MALFORMED_UPSTREAM_RESPONSE = "MALFORMED_UPSTREAM_RESPONSE"
    TRANSIENT_UPSTREAM_FAILURE = "TRANSIENT_UPSTREAM_FAILURE"
    CANCELLED = "CANCELLED"


class GitToolError(RemoteGitModel):
    code: GitErrorCode
    message: str
    retryable: bool = False


class GitToolResult(RemoteGitModel):
    contract_version: Literal[1] = 1
    tool_id: Literal["git.remote"] = "git.remote"
    operation: GitOperation
    logical_resource_id: RepoId
    outcome: Literal["success", "error"]
    data: GitData | None = None
    error: GitToolError | None = None
    correlation_id: str
    audit_ref: str

    @model_validator(mode="after")
    def exactly_one_payload(self) -> Self:
        if (self.data is None) == (self.error is None):
            raise ValueError("result requires exactly one of data or error")
        if (self.outcome == "success") != (self.data is not None):
            raise ValueError("outcome and payload disagree")
        return self


@dataclass(frozen=True, slots=True)
class TrustedGitContext:
    resource_context: TrustedResolutionContext
    initiative_revision: int | None = None
    approval_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    approved_commits: frozenset[tuple[str, str]] = frozenset()
    timeout_seconds: float = 10.0
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.resource_context, TrustedResolutionContext):
            raise TypeError("trusted resolution context required")
        if self.initiative_revision is not None and (
            type(self.initiative_revision) is not int or self.initiative_revision < 1
        ):
            raise ValueError("invalid initiative revision")
        if not isinstance(self.timeout_seconds, (int, float)) or not 0 < self.timeout_seconds <= 30:
            raise ValueError("timeout must be bounded")
        if type(self.cancelled) is not bool:
            raise TypeError("cancelled must be a bool")
        from re import fullmatch

        for repository_id, sha in self.approved_commits:
            if (
                fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", repository_id) is None
                or fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None
            ):
                raise ValueError("invalid trusted repository/commit handoff")
