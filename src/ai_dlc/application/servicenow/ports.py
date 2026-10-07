"""Provider and audit ports independent of ServiceNow transport and MCP SDK."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ai_dlc.application.resource_bindings import ServiceNowBinding
from ai_dlc.application.tool_policy import ServiceNowOperation

from .models import (
    AddServiceNowCommentRequest,
    CreateServiceNowRecordRequest,
    GetServiceNowRecordRequest,
    SearchServiceNowRecordsRequest,
    ServiceNowErrorCode,
    ServiceNowJournalResult,
    ServiceNowRecordDetail,
    ServiceNowSearchPage,
    ServiceNowWriteResult,
    UpdateServiceNowRecordRequest,
)


@dataclass(frozen=True, slots=True)
class ServiceNowProviderContext:
    """Trusted adapter-only aliases; the adapter resolves endpoint/auth separately."""

    binding: ServiceNowBinding
    correlation_id: str
    timeout_seconds: float


class ServiceNowProviderFailure(Exception):
    """Sanitized upstream failure. Its message is never copied into tool output."""

    def __init__(self, code: ServiceNowErrorCode) -> None:
        if code not in {
            ServiceNowErrorCode.RESOURCE_NOT_FOUND,
            ServiceNowErrorCode.UPSTREAM_AUTH_CONFIGURATION,
            ServiceNowErrorCode.UPSTREAM_TIMEOUT,
            ServiceNowErrorCode.MALFORMED_UPSTREAM_RESPONSE,
            ServiceNowErrorCode.TRANSIENT_UPSTREAM_FAILURE,
        }:
            raise ValueError("provider failure code must be an upstream error")
        self.code = code
        super().__init__(code.value)


class ServiceNowProvider(Protocol):
    def get_record(
        self, context: ServiceNowProviderContext, request: GetServiceNowRecordRequest
    ) -> ServiceNowRecordDetail: ...

    def search_records(
        self, context: ServiceNowProviderContext, request: SearchServiceNowRecordsRequest
    ) -> ServiceNowSearchPage: ...

    def create_record(
        self, context: ServiceNowProviderContext, request: CreateServiceNowRecordRequest
    ) -> ServiceNowWriteResult: ...

    def update_record(
        self, context: ServiceNowProviderContext, request: UpdateServiceNowRecordRequest
    ) -> ServiceNowWriteResult: ...

    def add_comment(
        self, context: ServiceNowProviderContext, request: AddServiceNowCommentRequest
    ) -> ServiceNowJournalResult: ...


@dataclass(frozen=True, slots=True)
class ServiceNowToolAuditEvent:
    audit_ref: str
    occurred_at: datetime
    correlation_id: str
    principal_id: str
    initiative_id: str
    workspace_id: str | None
    task_id: str | None
    operation: ServiceNowOperation
    scope_id: str | None
    record_type: str | None
    outcome: str
    error_code: ServiceNowErrorCode | None
    latency_ms: int
    policy_decision_id: str | None


class ServiceNowToolAuditSink(Protocol):
    def record(self, event: ServiceNowToolAuditEvent) -> None: ...
