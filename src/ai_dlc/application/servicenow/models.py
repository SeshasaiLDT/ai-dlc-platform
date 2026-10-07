"""Strict logical ServiceNow contracts; physical routing is trusted-only."""

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
from ai_dlc.application.tool_policy import ServiceNowOperation

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$", max_length=80)]
RecordId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=80)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
BodyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ServiceNowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordType(StrEnum):
    INCIDENT = "incident"
    REQUEST = "request"


class JournalChannel(StrEnum):
    COMMENT = "comment"
    WORK_NOTE = "work_note"


class ScopedRequest(ServiceNowModel):
    scope_id: Identifier
    record_type: RecordType

    @model_validator(mode="after")
    def matching_scope(self) -> Self:
        # Initial approved scopes are the two logical record types. No table names are accepted.
        if self.scope_id != self.record_type.value:
            raise ValueError("record type must match logical scope")
        return self


class GetServiceNowRecordRequest(ScopedRequest):
    record_id: RecordId


class SearchServiceNowRecordsRequest(ScopedRequest):
    number: RecordId | None = None
    state: ShortText | None = None
    text: ShortText | None = None
    assignment_group: ShortText | None = None
    page_size: int = Field(default=25, ge=1, le=100)
    cursor: Cursor | None = None


class CreateServiceNowRecordRequest(ScopedRequest):
    short_description: ShortText
    description: BodyText | None = None


class UpdateServiceNowRecordRequest(ScopedRequest):
    record_id: RecordId
    short_description: ShortText | None = None
    description: BodyText | None = None
    state: ShortText | None = None

    @model_validator(mode="after")
    def has_patch(self) -> Self:
        if all(value is None for value in (self.short_description, self.description, self.state)):
            raise ValueError("at least one update field is required")
        return self


class AddServiceNowCommentRequest(ScopedRequest):
    record_id: RecordId
    channel: JournalChannel
    body: BodyText


REQUEST_TYPES: dict[ServiceNowOperation, type[ServiceNowModel]] = {
    ServiceNowOperation.READ_RECORD: GetServiceNowRecordRequest,
    ServiceNowOperation.SEARCH: SearchServiceNowRecordsRequest,
    ServiceNowOperation.CREATE_RECORD: CreateServiceNowRecordRequest,
    ServiceNowOperation.UPDATE_RECORD: UpdateServiceNowRecordRequest,
    ServiceNowOperation.ADD_COMMENT: AddServiceNowCommentRequest,
}


class ServiceNowRecordSummary(ServiceNowModel):
    kind: Literal["record_summary"] = "record_summary"
    record_id: RecordId
    number: RecordId
    scope_id: Identifier
    record_type: RecordType
    short_description: ShortText
    state: ShortText
    priority: ShortText | None = None
    assignment_group: ShortText | None = None
    assignee_display: ShortText | None = None
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def matching_scope(self) -> Self:
        if self.scope_id != self.record_type.value:
            raise ValueError("record type must match logical scope")
        return self


class ServiceNowRecordDetail(ServiceNowRecordSummary):
    kind: Literal["record_detail"] = "record_detail"
    description: Annotated[str, StringConstraints(max_length=10000)] | None = None
    requester_display: ShortText | None = None


class ServiceNowSearchPage(ServiceNowModel):
    kind: Literal["search_page"] = "search_page"
    records: tuple[ServiceNowRecordSummary, ...]
    next_cursor: Cursor | None = None
    has_more: bool

    @model_validator(mode="after")
    def consistent_cursor(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("cursor and has_more disagree")
        return self


class ServiceNowWriteResult(ServiceNowModel):
    kind: Literal["write_result"] = "write_result"
    record_id: RecordId
    number: RecordId
    scope_id: Identifier
    record_type: RecordType
    state: ShortText


class ServiceNowJournalResult(ServiceNowModel):
    kind: Literal["journal_result"] = "journal_result"
    record_id: RecordId
    scope_id: Identifier
    record_type: RecordType
    channel: JournalChannel
    entry_id: RecordId
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value


ServiceNowData = Annotated[
    ServiceNowRecordDetail | ServiceNowSearchPage | ServiceNowWriteResult | ServiceNowJournalResult,
    Field(discriminator="kind"),
]


class ServiceNowErrorCode(StrEnum):
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_SCOPE = "INVALID_SCOPE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    UPSTREAM_AUTH_CONFIGURATION = "UPSTREAM_AUTH_CONFIGURATION"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    MALFORMED_UPSTREAM_RESPONSE = "MALFORMED_UPSTREAM_RESPONSE"
    TRANSIENT_UPSTREAM_FAILURE = "TRANSIENT_UPSTREAM_FAILURE"
    CANCELLED = "CANCELLED"


class ServiceNowToolError(ServiceNowModel):
    code: ServiceNowErrorCode
    message: str
    retryable: bool = False


class ServiceNowToolResult(ServiceNowModel):
    contract_version: Literal[1] = 1
    tool_id: Literal["servicenow"] = "servicenow"
    operation: ServiceNowOperation
    logical_resource_id: Literal["servicenow"] = "servicenow"
    outcome: Literal["success", "error"]
    data: ServiceNowData | None = None
    error: ServiceNowToolError | None = None
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
class TrustedServiceNowContext:
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
