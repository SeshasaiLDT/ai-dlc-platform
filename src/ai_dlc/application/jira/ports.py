"""Provider and sanitized audit ports independent of MCP and Jira SDKs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ai_dlc.application.resource_bindings import JiraBinding
from ai_dlc.application.tool_policy import JiraOperation

from .models import (
    AddJiraCommentRequest,
    CreateJiraIssueRequest,
    GetJiraIssueRequest,
    JiraCommentResult,
    JiraErrorCode,
    JiraIssueDetail,
    JiraSearchPage,
    JiraWriteResult,
    SearchJiraIssuesRequest,
    TransitionJiraIssueRequest,
    UpdateJiraIssueRequest,
)


@dataclass(frozen=True, slots=True)
class JiraProviderContext:
    """Trusted adapter-only routing. No endpoint or credential is exposed."""

    binding: JiraBinding
    correlation_id: str
    timeout_seconds: float


class JiraProviderFailure(Exception):
    """Sanitized provider failure; never use its message in an agent response."""

    def __init__(self, code: JiraErrorCode) -> None:
        if code not in {
            JiraErrorCode.RESOURCE_NOT_FOUND,
            JiraErrorCode.UPSTREAM_AUTH_CONFIGURATION,
            JiraErrorCode.UPSTREAM_TIMEOUT,
            JiraErrorCode.MALFORMED_UPSTREAM_RESPONSE,
            JiraErrorCode.TRANSIENT_UPSTREAM_FAILURE,
        }:
            raise ValueError("provider failure code is not an upstream error")
        self.code = code
        super().__init__(code.value)


class JiraProvider(Protocol):
    def get_issue(
        self, context: JiraProviderContext, request: GetJiraIssueRequest
    ) -> JiraIssueDetail: ...

    def search_issues(
        self, context: JiraProviderContext, request: SearchJiraIssuesRequest
    ) -> JiraSearchPage: ...

    def create_issue(
        self, context: JiraProviderContext, request: CreateJiraIssueRequest
    ) -> JiraWriteResult: ...

    def update_issue(
        self, context: JiraProviderContext, request: UpdateJiraIssueRequest
    ) -> JiraWriteResult: ...

    def transition_issue(
        self, context: JiraProviderContext, request: TransitionJiraIssueRequest
    ) -> JiraWriteResult: ...

    def add_comment(
        self, context: JiraProviderContext, request: AddJiraCommentRequest
    ) -> JiraCommentResult: ...


@dataclass(frozen=True, slots=True)
class JiraToolAuditEvent:
    audit_ref: str
    occurred_at: datetime
    correlation_id: str
    principal_id: str
    initiative_id: str
    workspace_id: str | None
    task_id: str | None
    operation: JiraOperation
    project_key: str | None
    outcome: str
    error_code: JiraErrorCode | None
    latency_ms: int
    policy_decision_id: str | None


class JiraToolAuditSink(Protocol):
    def record(self, event: JiraToolAuditEvent) -> None: ...
