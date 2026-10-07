"""Finite local workspace operations and path-free public schemas."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from re import fullmatch
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
RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=512)]
PatchText = Annotated[str, StringConstraints(min_length=1, max_length=65536)]
Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


def valid_branch(value: str) -> bool:
    return ".." not in value and not value.endswith(("/", ".")) and not value.startswith("-")


def valid_relative_path(value: str) -> bool:
    return (
        fullmatch(r"[A-Za-z0-9._/-]+", value) is not None
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in ("", ".", "..", ".git") for part in value.split("/"))
        and all(ord(char) >= 32 for char in value)
    )


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalWorkspaceOperation(StrEnum):
    PREPARE = "prepare"
    CHECKOUT = "checkout"
    STATUS = "status"
    DIFF = "diff"
    APPLY_PATCH = "apply_patch"
    BUILD = "build"
    TEST = "test"
    COMMIT = "commit"
    CLEANUP = "cleanup"


POLICY_OPERATIONS: dict[LocalWorkspaceOperation, GitOperation] = {
    LocalWorkspaceOperation.PREPARE: GitOperation.PREPARE_WORKSPACE,
    LocalWorkspaceOperation.CHECKOUT: GitOperation.LOCAL_CHECKOUT,
    LocalWorkspaceOperation.STATUS: GitOperation.LOCAL_STATUS,
    LocalWorkspaceOperation.DIFF: GitOperation.LOCAL_DIFF,
    LocalWorkspaceOperation.APPLY_PATCH: GitOperation.LOCAL_APPLY_PATCH,
    LocalWorkspaceOperation.BUILD: GitOperation.LOCAL_BUILD,
    LocalWorkspaceOperation.TEST: GitOperation.LOCAL_TEST,
    LocalWorkspaceOperation.COMMIT: GitOperation.COMMIT,
    LocalWorkspaceOperation.CLEANUP: GitOperation.LOCAL_CLEANUP,
}


class RepoRequest(WorkspaceModel):
    repository_id: RepoId


class PrepareWorkspaceRequest(RepoRequest):
    pass


class CheckoutWorkspaceRequest(RepoRequest):
    branch: Branch

    @field_validator("branch")
    @classmethod
    def safe_branch(cls, value: str) -> str:
        if not valid_branch(value):
            raise ValueError("invalid branch")
        return value


class StatusWorkspaceRequest(RepoRequest):
    pass


class DiffWorkspaceRequest(RepoRequest):
    pass


class ApplyPatchRequest(RepoRequest):
    patch: PatchText

    @field_validator("patch")
    @classmethod
    def bounded_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("patch exceeds byte limit")
        return value


class BuildWorkspaceRequest(RepoRequest):
    build_profile_id: RepoId


class TestWorkspaceRequest(RepoRequest):
    build_profile_id: RepoId


class CommitWorkspaceRequest(RepoRequest):
    message: Message
    paths: tuple[RelativePath, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def safe_paths(self) -> Self:
        if len(set(self.paths)) != len(self.paths) or any(
            not valid_relative_path(path) for path in self.paths
        ):
            raise ValueError("invalid commit paths")
        return self


class CleanupWorkspaceRequest(RepoRequest):
    pass


REQUEST_TYPES: dict[LocalWorkspaceOperation, type[WorkspaceModel]] = {
    LocalWorkspaceOperation.PREPARE: PrepareWorkspaceRequest,
    LocalWorkspaceOperation.CHECKOUT: CheckoutWorkspaceRequest,
    LocalWorkspaceOperation.STATUS: StatusWorkspaceRequest,
    LocalWorkspaceOperation.DIFF: DiffWorkspaceRequest,
    LocalWorkspaceOperation.APPLY_PATCH: ApplyPatchRequest,
    LocalWorkspaceOperation.BUILD: BuildWorkspaceRequest,
    LocalWorkspaceOperation.TEST: TestWorkspaceRequest,
    LocalWorkspaceOperation.COMMIT: CommitWorkspaceRequest,
    LocalWorkspaceOperation.CLEANUP: CleanupWorkspaceRequest,
}


class WorkspaceState(StrEnum):
    ACTIVE = "active"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkspaceKey:
    initiative_id: str
    workspace_id: str
    task_id: str
    repository_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """Trusted-only root; never serialize into an agent result."""

    key: WorkspaceKey
    owner_principal_id: str
    root: Path
    home: Path
    state: WorkspaceState
    branch: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TrustedCommitHandoff:
    initiative_id: str
    workspace_id: str
    task_id: str
    repository_id: str
    principal_id: str
    commit_sha: str
    created_at: datetime


class WorkspaceInfo(WorkspaceModel):
    kind: Literal["workspace"] = "workspace"
    repository_id: RepoId
    state: WorkspaceState
    branch: Branch | None = None


class LocalStatus(WorkspaceModel):
    kind: Literal["status"] = "status"
    source: Literal["local_workspace"] = "local_workspace"
    repository_id: RepoId
    branch: Branch
    head_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")]
    staged_files: tuple[RelativePath, ...] = Field(max_length=100)
    modified_files: tuple[RelativePath, ...] = Field(max_length=100)
    untracked_files: tuple[RelativePath, ...] = Field(max_length=100)
    omitted_file_count: int = Field(ge=0)
    truncated: bool
    clean: bool

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.truncated != (self.omitted_file_count > 0):
            raise ValueError("status truncation disagrees with omitted count")
        if self.clean != (
            not self.staged_files
            and not self.modified_files
            and not self.untracked_files
            and self.omitted_file_count == 0
        ):
            raise ValueError("status clean flag disagrees with files")
        return self


class LocalDiff(WorkspaceModel):
    kind: Literal["diff"] = "diff"
    source: Literal["local_workspace"] = "local_workspace"
    repository_id: RepoId
    patch: Annotated[str, StringConstraints(max_length=32768)]
    changed_files: tuple[RelativePath, ...] = Field(max_length=100)
    omitted_file_count: int = Field(ge=0)
    truncated: bool


class PatchResult(WorkspaceModel):
    kind: Literal["patch"] = "patch"
    repository_id: RepoId
    changed_files: tuple[RelativePath, ...] = Field(max_length=20)


class ProcessResult(WorkspaceModel):
    kind: Literal["process"] = "process"
    repository_id: RepoId
    operation: Literal["build", "test"]
    exit_code: int
    success: bool
    duration_ms: int = Field(ge=0)
    stdout: Annotated[str, StringConstraints(max_length=8192)]
    stderr: Annotated[str, StringConstraints(max_length=8192)]
    stdout_truncated: bool
    stderr_truncated: bool


class CommitResult(WorkspaceModel):
    kind: Literal["commit"] = "commit"
    repository_id: RepoId
    commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")]


WorkspaceData = Annotated[
    WorkspaceInfo | LocalStatus | LocalDiff | PatchResult | ProcessResult | CommitResult,
    Field(discriminator="kind"),
]


class WorkspaceErrorCode(StrEnum):
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_SCOPE = "INVALID_SCOPE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_NOT_ACTIVE = "WORKSPACE_NOT_ACTIVE"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    PATCH_REJECTED = "PATCH_REJECTED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    CANCELLED = "CANCELLED"
    RUNTIME_CONFIGURATION = "RUNTIME_CONFIGURATION"


class WorkspaceToolError(WorkspaceModel):
    code: WorkspaceErrorCode
    message: str
    retryable: bool = False


class WorkspaceToolResult(WorkspaceModel):
    contract_version: Literal[1] = 1
    tool_id: Literal["workspace.local"] = "workspace.local"
    operation: LocalWorkspaceOperation
    logical_resource_id: RepoId
    outcome: Literal["success", "error"]
    data: WorkspaceData | None = None
    error: WorkspaceToolError | None = None
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
class TrustedWorkspaceContext:
    resource_context: TrustedResolutionContext
    workspace_id: str
    task_id: str
    initiative_revision: int | None = None
    approval_id: str | None = None
    timeout_seconds: float = 10.0
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.resource_context, TrustedResolutionContext):
            raise TypeError("trusted resource context required")
        for value in (self.workspace_id, self.task_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 80:
                raise ValueError("trusted workspace/task ID required")
        if self.initiative_revision is not None and (
            type(self.initiative_revision) is not int or self.initiative_revision < 1
        ):
            raise ValueError("invalid initiative revision")
        if not isinstance(self.timeout_seconds, (int, float)) or not 0 < self.timeout_seconds <= 60:
            raise ValueError("timeout must be bounded")
        if type(self.cancelled) is not bool:
            raise TypeError("cancelled must be a bool")
