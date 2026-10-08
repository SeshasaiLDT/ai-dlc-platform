"""Strict logical Jira requests and normalized agent-visible results."""

from dataclasses import dataclass
from datetime import datetime
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
from ai_dlc.application.tool_policy import JiraOperation

ProjectKey = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=20)]
IssueKey = Annotated[
    str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$", max_length=40)
]
BoundedText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
OpaqueCursor = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class JiraModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GetJiraIssueRequest(JiraModel):
    project_key: ProjectKey
    issue_key: IssueKey


class SearchJiraIssuesRequest(JiraModel):
    project_key: ProjectKey
    text: ShortText | None = None
    status: ShortText | None = None
    issue_type: ShortText | None = None
    assignee: ShortText | None = None
    labels: tuple[ShortText, ...] = Field(default=(), max_length=20)
    page_size: int = Field(default=25, ge=1, le=100)
    cursor: OpaqueCursor | None = None


class CreateJiraIssueRequest(JiraModel):
    project_key: ProjectKey
    issue_type: ShortText
    summary: ShortText
    description: BoundedText | None = None


class UpdateJiraIssueRequest(JiraModel):
    project_key: ProjectKey
    issue_key: IssueKey
    summary: ShortText | None = None
    description: BoundedText | None = None

    @model_validator(mode="after")
    def has_patch(self) -> Self:
        if self.summary is None and self.description is None:
            raise ValueError("at least one update field is required")
        return self


class TransitionJiraIssueRequest(JiraModel):
    project_key: ProjectKey
    issue_key: IssueKey
    transition_id: ShortText


class AddJiraCommentRequest(JiraModel):
    project_key: ProjectKey
    issue_key: IssueKey
    body: BoundedText


JiraRequest = (
    GetJiraIssueRequest
    | SearchJiraIssuesRequest
    | CreateJiraIssueRequest
    | UpdateJiraIssueRequest
    | TransitionJiraIssueRequest
    | AddJiraCommentRequest
)

REQUEST_TYPES: dict[JiraOperation, type[JiraModel]] = {
    JiraOperation.READ_ISSUE: GetJiraIssueRequest,
    JiraOperation.SEARCH: SearchJiraIssuesRequest,
    JiraOperation.CREATE_ISSUE: CreateJiraIssueRequest,
    JiraOperation.UPDATE_ISSUE: UpdateJiraIssueRequest,
    JiraOperation.TRANSITION_ISSUE: TransitionJiraIssueRequest,
    JiraOperation.ADD_COMMENT: AddJiraCommentRequest,
}


class JiraIssueSummary(JiraModel):
    kind: Literal["issue_summary"] = "issue_summary"
    issue_key: IssueKey
    project_key: ProjectKey
    issue_type: ShortText
    summary: ShortText
    status: ShortText
    priority: ShortText | None = None
    assignee_display: ShortText | None = None
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def matching_project(self) -> Self:
        if self.issue_key.split("-", 1)[0] != self.project_key:
            raise ValueError("issue key and project disagree")
        return self


class JiraIssueDetail(JiraIssueSummary):
    kind: Literal["issue_detail"] = "issue_detail"
    description: Annotated[str, StringConstraints(max_length=10000)] | None = None
    labels: tuple[ShortText, ...] = Field(default=(), max_length=50)
    linked_issue_keys: tuple[IssueKey, ...] = Field(default=(), max_length=50)


class JiraSearchPage(JiraModel):
    kind: Literal["search_page"] = "search_page"
    issues: tuple[JiraIssueSummary, ...]
    next_cursor: OpaqueCursor | None = None
    has_more: bool

    @model_validator(mode="after")
    def cursor_consistent(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("pagination cursor and has_more disagree")
        return self


class JiraWriteResult(JiraModel):
    kind: Literal["write_result"] = "write_result"
    issue_key: IssueKey
    project_key: ProjectKey
    status: ShortText

    @model_validator(mode="after")
    def matching_project(self) -> Self:
        if self.issue_key.split("-", 1)[0] != self.project_key:
            raise ValueError("issue key and project disagree")
        return self


class JiraCommentResult(JiraModel):
    kind: Literal["comment_result"] = "comment_result"
    issue_key: IssueKey
    project_key: ProjectKey
    comment_id: ShortText
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


JiraData = Annotated[
    JiraIssueDetail | JiraSearchPage | JiraWriteResult | JiraCommentResult,
    Field(discriminator="kind"),
]


class JiraErrorCode(StrEnum):
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_SCOPE = "INVALID_SCOPE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    UPSTREAM_AUTH_CONFIGURATION = "UPSTREAM_AUTH_CONFIGURATION"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    MALFORMED_UPSTREAM_RESPONSE = "MALFORMED_UPSTREAM_RESPONSE"
    TRANSIENT_UPSTREAM_FAILURE = "TRANSIENT_UPSTREAM_FAILURE"
    CANCELLED = "CANCELLED"


class JiraToolError(JiraModel):
    code: JiraErrorCode
    message: str
    retryable: bool = False


class JiraToolResult(JiraModel):
    contract_version: Literal[1] = 1
    tool_id: Literal["jira"] = "jira"
    operation: JiraOperation
    logical_resource_id: Literal["jira"] = "jira"
    outcome: Literal["success", "error"]
    data: JiraData | None = None
    error: JiraToolError | None = None
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
class TrustedJiraContext:
    """Server-only invocation fields; never parse these from MCP arguments."""

    resource_context: TrustedResolutionContext
    initiative_revision: int | None = None
    approval_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    timeout_seconds: float = 10.0
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.resource_context, TrustedResolutionContext):
            raise TypeError("trusted resource context required")
        if self.initiative_revision is not None and (
            type(self.initiative_revision) is not int or self.initiative_revision < 1
        ):
            raise ValueError("invalid initiative revision")
        if not isinstance(self.timeout_seconds, (int, float)) or not 0 < self.timeout_seconds <= 30:
            raise ValueError("timeout must be bounded")
        if type(self.cancelled) is not bool:
            raise TypeError("cancelled must be a bool")
        for value in (self.approval_id, self.workspace_id, self.task_id):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("trusted context ID must be nonblank")
