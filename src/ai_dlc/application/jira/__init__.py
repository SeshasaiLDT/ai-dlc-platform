"""Governed Jira integration contracts."""

from .mcp import JIRA_MCP_TOOLS, JiraMcpToolHandler
from .models import (
    AddJiraCommentRequest,
    CreateJiraIssueRequest,
    GetJiraIssueRequest,
    JiraCommentResult,
    JiraErrorCode,
    JiraIssueDetail,
    JiraIssueSummary,
    JiraSearchPage,
    JiraToolResult,
    JiraWriteResult,
    SearchJiraIssuesRequest,
    TransitionJiraIssueRequest,
    TrustedJiraContext,
    UpdateJiraIssueRequest,
)
from .ports import JiraProvider, JiraProviderContext, JiraProviderFailure, JiraToolAuditEvent
from .service import GovernedJiraService, JiraAuditError, JiraCancelled

__all__ = [
    "AddJiraCommentRequest",
    "CreateJiraIssueRequest",
    "GetJiraIssueRequest",
    "GovernedJiraService",
    "JIRA_MCP_TOOLS",
    "JiraAuditError",
    "JiraCancelled",
    "JiraCommentResult",
    "JiraErrorCode",
    "JiraIssueDetail",
    "JiraIssueSummary",
    "JiraMcpToolHandler",
    "JiraProvider",
    "JiraProviderContext",
    "JiraProviderFailure",
    "JiraSearchPage",
    "JiraToolAuditEvent",
    "JiraToolResult",
    "JiraWriteResult",
    "SearchJiraIssuesRequest",
    "TransitionJiraIssueRequest",
    "TrustedJiraContext",
    "UpdateJiraIssueRequest",
]
