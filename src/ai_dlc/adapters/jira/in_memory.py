"""Deterministic fake Jira provider and audit sink for local/contract tests."""

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

from ai_dlc.application.jira.models import (
    AddJiraCommentRequest,
    CreateJiraIssueRequest,
    GetJiraIssueRequest,
    JiraCommentResult,
    JiraErrorCode,
    JiraIssueDetail,
    JiraIssueSummary,
    JiraSearchPage,
    JiraWriteResult,
    SearchJiraIssuesRequest,
    TransitionJiraIssueRequest,
    UpdateJiraIssueRequest,
)
from ai_dlc.application.jira.ports import (
    JiraProviderContext,
    JiraProviderFailure,
    JiraToolAuditEvent,
)


@dataclass(frozen=True, slots=True)
class ProviderCall:
    operation: str
    connection_alias: str
    site_alias: str
    project_key: str


class InMemoryJiraProvider:
    def __init__(self, issues: tuple[JiraIssueDetail, ...] = ()) -> None:
        self._issues = {issue.issue_key: issue for issue in issues}
        self.calls: list[ProviderCall] = []
        self.fail_next: JiraErrorCode | None = None

    def _record(self, operation: str, context: JiraProviderContext, project_key: str) -> None:
        self.calls.append(
            ProviderCall(
                operation, context.binding.connection_alias, context.binding.site_alias, project_key
            )
        )
        if self.fail_next is not None:
            code, self.fail_next = self.fail_next, None
            raise JiraProviderFailure(code)

    def get_issue(
        self, context: JiraProviderContext, request: GetJiraIssueRequest
    ) -> JiraIssueDetail:
        self._record("get_issue", context, request.project_key)
        try:
            return self._issues[request.issue_key]
        except KeyError:
            raise JiraProviderFailure(JiraErrorCode.RESOURCE_NOT_FOUND) from None

    def search_issues(
        self, context: JiraProviderContext, request: SearchJiraIssuesRequest
    ) -> JiraSearchPage:
        self._record("search_issues", context, request.project_key)
        matches = [
            issue
            for issue in sorted(self._issues.values(), key=lambda item: item.issue_key)
            if issue.project_key == request.project_key
            and (
                request.text is None
                or request.text.casefold()
                in (issue.summary + " " + (issue.description or "")).casefold()
            )
            and (request.status is None or issue.status == request.status)
            and (request.issue_type is None or issue.issue_type == request.issue_type)
            and (request.assignee is None or issue.assignee_display == request.assignee)
            and all(label in issue.labels for label in request.labels)
        ]
        try:
            start = int(request.cursor) if request.cursor is not None else 0
        except ValueError:
            raise JiraProviderFailure(JiraErrorCode.MALFORMED_UPSTREAM_RESPONSE) from None
        if start < 0:
            raise JiraProviderFailure(JiraErrorCode.MALFORMED_UPSTREAM_RESPONSE)
        page = matches[start : start + request.page_size]
        next_offset = start + len(page)
        has_more = next_offset < len(matches)
        return JiraSearchPage(
            issues=tuple(
                JiraIssueSummary.model_validate(
                    issue.model_dump(exclude={"kind", "description", "labels", "linked_issue_keys"})
                )
                for issue in page
            ),
            next_cursor=str(next_offset) if has_more else None,
            has_more=has_more,
        )

    def create_issue(
        self, context: JiraProviderContext, request: CreateJiraIssueRequest
    ) -> JiraWriteResult:
        self._record("create_issue", context, request.project_key)
        numbers = [
            int(key.rsplit("-", 1)[1])
            for key in self._issues
            if key.startswith(request.project_key + "-")
        ]
        issue_key = f"{request.project_key}-{max(numbers, default=0) + 1}"
        self._issues[issue_key] = JiraIssueDetail(
            issue_key=issue_key,
            project_key=request.project_key,
            issue_type=request.issue_type,
            summary=request.summary,
            status="Open",
            updated_at=datetime.now(UTC),
            description=request.description,
        )
        return JiraWriteResult(issue_key=issue_key, project_key=request.project_key, status="Open")

    def update_issue(
        self, context: JiraProviderContext, request: UpdateJiraIssueRequest
    ) -> JiraWriteResult:
        self._record("update_issue", context, request.project_key)
        issue = self._existing(request.issue_key)
        updated = issue.model_copy(
            update={
                "summary": request.summary if request.summary is not None else issue.summary,
                "description": request.description
                if request.description is not None
                else issue.description,
                "updated_at": datetime.now(UTC),
            }
        )
        self._issues[request.issue_key] = updated
        return JiraWriteResult(
            issue_key=updated.issue_key, project_key=updated.project_key, status=updated.status
        )

    def transition_issue(
        self, context: JiraProviderContext, request: TransitionJiraIssueRequest
    ) -> JiraWriteResult:
        self._record("transition_issue", context, request.project_key)
        issue = self._existing(request.issue_key)
        updated = issue.model_copy(
            update={"status": request.transition_id, "updated_at": datetime.now(UTC)}
        )
        self._issues[request.issue_key] = updated
        return JiraWriteResult(
            issue_key=updated.issue_key, project_key=updated.project_key, status=updated.status
        )

    def add_comment(
        self, context: JiraProviderContext, request: AddJiraCommentRequest
    ) -> JiraCommentResult:
        self._record("add_comment", context, request.project_key)
        self._existing(request.issue_key)
        return JiraCommentResult(
            issue_key=request.issue_key,
            project_key=request.project_key,
            comment_id=f"comment-{len(self.calls)}",
            created_at=datetime.now(UTC),
        )

    def _existing(self, issue_key: str) -> JiraIssueDetail:
        try:
            return self._issues[issue_key]
        except KeyError:
            raise JiraProviderFailure(JiraErrorCode.RESOURCE_NOT_FOUND) from None


class InMemoryJiraToolAuditSink:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[JiraToolAuditEvent] = []

    def record(self, event: JiraToolAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[JiraToolAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
